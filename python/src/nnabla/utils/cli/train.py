# Copyright (c) 2017 Sony Corporation. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from six.moves import range
from collections import OrderedDict
from contextlib2 import ExitStack  # Backport from python3
import numpy as np
import glob
import os
import time
import zipfile
import tempfile
import time
from shutil import rmtree

import nnabla.communicators as C
import nnabla as nn
from nnabla.logger import logger
from nnabla import available_contexts
from nnabla.parameter import save_parameters, load_parameters
from nnabla.utils.progress import configure_progress, progress
from nnabla.utils.cli.utility import let_data_to_variable
from nnabla.utils.nnp_format import nnp_version
from nnabla.parameter import get_parameter_or_create

import nnabla.utils.load as load

_save_parameter_info = {}

def _save_parameters(args, suffix, epoch, force=False):
    global _save_parameter_info

    if suffix not in _save_parameter_info:
        _save_parameter_info[suffix] = {}
        _save_parameter_info[suffix]['epoch'] = 0
        _save_parameter_info[suffix]['time'] = 0

    current_time = time.time()
    timediff = current_time - _save_parameter_info[suffix]['time']
    epochdiff = epoch - _save_parameter_info[suffix]['epoch']

    globname = os.path.join(args.outdir, 'results_{}_*.nnp'.format(suffix))
    exists = glob.glob(globname)

    base = os.path.join(args.outdir, 'results_{}_{}'.format(suffix, epoch))
    filename = base + '.nnp'

    if not os.path.exists(filename) and \
       (force or timediff > 180.0 or epochdiff > 10):

        version_filename = base + '_version.txt'

        with open(version_filename, 'w') as file:
            file.write('{}\n'.format(nnp_version()))

        param_filename = base + '_param.protobuf'
        save_parameters(param_filename)

        with zipfile.ZipFile(filename, 'w') as nnp:
            nnp.write(version_filename, 'nnp_version.txt')
            nnp.write(_save_parameter_info['config'], os.path.basename(
                _save_parameter_info['config']))
            nnp.write(param_filename, 'parameter.protobuf')

        os.unlink(version_filename)
        os.unlink(param_filename)

        for exist in exists:
            os.unlink(exist)

        _save_parameter_info[suffix]['epoch'] = epoch
        _save_parameter_info[suffix]['time'] = current_time


def _update(iter, config, cost):
    comm = C.CurrentCommunicator()

    loaded_datas = {}
    is_first_optimizer = True

    def sum_cost(sum_iter):
        if comm:
            cost_sum_iter = np.zeros(1)
            cost_sum_iter[0] = sum_iter
            cost_sum_epoch = np.zeros(1)
            #MPI.COMM_WORLD.Allreduce(cost_sum_iter, cost_sum_epoch, op=MPI.SUM)
            cost.sum_epoch += cost_sum_epoch[0]
            cost.num_iter += comm.size
        else:
            cost.sum_epoch += sum_iter
            cost.num_iter += 1

    for opt in config.optimizers.values():
        o = opt.optimizer
        # Load dataset
        di = opt.data_iterator
        if o.data_iterator not in loaded_datas:
            loaded_datas[o.data_iterator] = di.next()
        datas = loaded_datas[o.data_iterator]
        for v, d in o.dataset_assign.items():
            dest_context = config.global_config.default_context if not o.forward_sequence or v not in o.forward_sequence[
                0].inputs else None
            let_data_to_variable(v.variable_instance, datas[
                                 di.variables.index(d)], ctx=dest_context)

        # Generate data
        for v, generator in o.generator_assign.items():
            dest_context = config.global_config.default_context if not o.forward_sequence or v not in o.forward_sequence[
                0].inputs else None
            let_data_to_variable(v.variable_instance,
                                 data=generator(v.shape), ctx=dest_context)

        # Monitor loss before forward to prepare input data while processing on
        # GPU
        if cost.variables:
            for l in cost.variables:
                cost.sum_iter += np.mean(l.variable_instance.d)
            if is_first_optimizer:
                is_first_optimizer = False
                if not comm or comm.rank == 0:
                    progress("Training : cost={0:0.6f}".format(cost.sum_iter),
                             (iter % config.training_config.iter_per_epoch) * 1.0 / config.training_config.iter_per_epoch)
                sum_cost(cost.sum_iter)
                cost.sum_iter = 0.0

        # Forward
        o.network.forward(o.forward_sequence)

        # Backward
        o.network.backward(o.backward_sequence, iter % o.update_interval == 0)

        # Update
        if iter % o.update_interval == o.update_interval - 1:
            if o.weight_decay > 0:
                o.solver.weight_decay(o.weight_decay)
            if o.comm:
                params = [x.grad for x in nn.get_parameters().values()]
                o.comm.all_reduce(params, division=False, inplace=False)
            o.solver.update()

        if o.lr_decay != 1.0 and iter % o.lr_decay_interval == o.lr_decay_interval - 1:
            o.solver.set_learning_rate(o.solver.learning_rate() * o.lr_decay)

        # Reserve monitor loss
        cost.variables = o.loss_variables

    # Monitor loss at the end of iteration
    if iter % config.training_config.iter_per_epoch == config.training_config.iter_per_epoch - 1 and cost.variables:
        for l in cost.variables:
            cost.sum_iter += np.mean(l.variable_instance.d)
        sum_cost(cost.sum_iter)
        cost.variables = None
        cost.sum_iter = 0.0

    return cost


def _evaluate(args, config, monitoring_report, best_error, epoch):
    comm = C.CurrentCommunicator()
    error_str = ''
    valid_error = 0.0

    def sum_error(sum, error):
        if comm:
            error_buf = np.zeros(1)
            error_buf[0] = error
            error_sum = np.zeros(1)
            #MPI.COMM_WORLD.Allreduce(error_buf, error_sum, op=MPI.SUM)
            return sum + error_sum[0]
        else:
            return sum + error

    for name, mon in config.monitors.items():
        m = mon.monitor
        error_sum_monitor = 0.0
        error_count = 0
        di = mon.data_iterator

        for i in range(di.size // di.batch_size):
            # Set data to variable
            datas = di.next()
            for v, d in m.dataset_assign.items():
                dest_context = config.global_config.default_context if not m.forward_sequence or v not in m.forward_sequence[
                    0].inputs else None
                let_data_to_variable(v.variable_instance, datas[
                                     di.variables.index(d)], ctx=dest_context)

            # Generate data
            for v, generator in m.generator_assign.items():
                dest_context = config.global_config.default_context if not m.forward_sequence or v not in m.forward_sequence[
                    0].inputs else None
                let_data_to_variable(v.variable_instance,
                                     data=generator(v.shape), ctx=dest_context)

            # Sum error before forward to prepare input data while processing
            # on GPU
            if error_count > 0:
                error_sum = 0.0
                for v in m.monitor_variables:
                    error_sum += np.mean(v.variable_instance.d)
                error_sum_monitor = sum_error(error_sum_monitor, error_sum)
                if not comm or comm.rank == 0:
                    progress('Evaluating "{0}"'.format(
                        name) + ' : error={0:0.6f}'.format(
                        error_sum_monitor / error_count),
                        di.position * 1.0 / di.size)
            error_count += comm.size if comm else 1

            # Forward recursive
            m.network.forward(m.forward_sequence)

        # Sum error at the end of dataset
        error_sum = 0.0
        for v in m.monitor_variables:
            error_sum += np.mean(v.variable_instance.d)
        error_sum_monitor = sum_error(error_sum_monitor, error_sum)

        if error_count == 0:
            error_count = 1
        error = error_sum_monitor / error_count
        monitoring_report.append('  {}: {}\n'.format(name, error))
        if error_str != '':
            error_str += ', '
        else:
            error_str = ' {'
        error_str += '{}={:.6f}'.format(name, error)
        if name == 'valid_error':
            valid_error = error
    if error_str != '':
        error_str += '}'

    # Save Parameters
    if not comm or comm.rank == 0:
        if (not config.training_config.save_best) or \
           (not best_error) or \
           (best_error is not None and valid_error <= best_error):
            best_error = valid_error
            _save_parameters(args, 'best', epoch, True)

    return best_error, error_str


def _get_current_parameter(args):

    globname = os.path.join(args.outdir, 'results_current_*.nnp')
    exists = glob.glob(globname)

    if len(exists) > 0:
        ex_list = {}

        for ex in exists:
            n = int(ex.rsplit('_', 1)[1].rsplit('.', 1)[0])
            ex_list[n] = ex

        last_epoch = sorted(ex_list.keys())[0]
        last_parameter = ex_list[last_epoch]
        logger.log(99, "Load parameter from [{}]".format(last_parameter))
        load.load([last_parameter], parameter_only=True)
        return last_epoch

    return 0


def train(args, config):
    global _save_parameter_info
    comm = C.CurrentCommunicator()

    last_epoch = 0
    if args.resume:
        last_epoch = _get_current_parameter(args)
        logger.log(99, "Resume from epoch {}".format(last_epoch))

    max_iter = config.training_config.max_epoch * \
        config.training_config.iter_per_epoch
    if not comm or comm.rank == 0:
        logger.log(99, 'Training epoch 1 of {} begin'.format(
            config.training_config.max_epoch))

    class Cost:
        pass
    cost = Cost()
    cost.sum_epoch = 0.0
    cost.num_iter = 0
    cost.sum_iter = 0.0
    cost.variables = None

    best_error = None

    if max_iter > 0:
        last_iter = last_epoch * config.training_config.iter_per_epoch
        if last_iter < max_iter:

            start_time = time.time()
            last_past_time = -1

            for iter in range(last_iter, max_iter):
                cost = _update(iter, config, cost)
                current_time = time.time()
                if (iter - last_iter) > 0:
                    past_time = current_time - start_time
                    if last_past_time < 0 or past_time - last_past_time > 5.0:
                        estimate_time = past_time * \
                            (max_iter - last_iter) / (iter - last_iter)
                        remain_time = estimate_time - past_time
                        # logger.log(99, 'time:{} remain:{} estimate:{}'.format(
                        #     past_time, remain_time, estimate_time))
                        if config.timelimit > 0 and estimate_time > config.timelimit:
                            logger.log(99, 'Expected training time ({:.3f}s) will exceed time limit ({}s).'.format(
                                estimate_time, config.timelimit))
                            return False
                        last_past_time = past_time

                if (iter + 1) % config.training_config.iter_per_epoch == 0:
                    last_past_time = -1
                    # End of epoch
                    epoch = iter // config.training_config.iter_per_epoch + 1
                    cost_avg_epoch = cost.sum_epoch / cost.num_iter
                    cost.sum_epoch = 0.0
                    cost.num_iter = 0
                    monitoring_report = []

                    # Evaluation
                    error_str = ''
                    if epoch % 10 == 0 or epoch <= 5:
                        best_error, error_str = _evaluate(
                            args, config, monitoring_report, best_error, epoch)

                    if not comm or comm.rank == 0:
                        # Write to monitoring_report.yml
                        f = open(os.path.join(
                            args.outdir, 'monitoring_report.yml'), 'a')
                        f.write('{}:\n'.format(epoch - 1))
                        f.write('  cost: {}\n'.format(cost_avg_epoch))
                        for str in monitoring_report:
                            f.write(str)
                        f.close()
                        _save_parameters(args, 'current', epoch)

                        logger.log(99, 'epoch {} of {} cost={:.6f} {}'.format(
                            epoch, config.training_config.max_epoch, cost_avg_epoch, error_str))

            if not comm or comm.rank == 0:
                _save_parameters(args, 'current', epoch, True)

    return True


def get_best_param(paramlist):
    h5list = []
    bestlist = {}
    currentlist = {}
    for fn in paramlist:
        name, ext = os.path.splitext(fn)
        if ext == '.h5':
            h5.append(ext)
        elif ext == '.nnp':
            ns = name.split('_')
            if len(ns) == 3:
                if ns[0] == 'results':
                    if ns[1] == 'best':
                        bestlist[int(ns[2])] = fn
                    elif ns[1] == 'current':
                        currentlist[int(ns[2])] = fn
    if len(bestlist) > 0:
        return bestlist[sorted(bestlist.keys()).pop()]
    elif len(currentlist) > 0:
        return currentlist[sorted(currentlist.keys()).pop()]
    elif len(h5list) > 0:
        return sorted(h5list).pop()
    return None


def train_command(args):
    configure_progress(os.path.join(args.outdir, 'progress.txt'))
    info = load.load([args.config], exclude_parameter=True)

    # Check dataset uri is empty.
    dataset_error = False
    for dataset in info.datasets.values():
        if dataset.uri.strip() == '':
            dataset_error = True
    if dataset_error:
        logger.log(99, 'Fatal error. Dataset URI is empty.')
        return

    class TrainConfig:
        pass
    config = TrainConfig()
    config.timelimit = -1
    if args.param:
        load.load([args.param], parameter_only=True)

    if args.sdcproj and args.job_url_list:
        job_url_list = {}
        with open(args.job_url_list) as f:
            for line in f.readlines():
                ls = line.strip().split()
                if len(ls) == 2:
                    job_url_list[ls[0]] = ls[1]

        param_list = {}
        with open(args.sdcproj) as f:
            is_file_property = False
            for line in f.readlines():
                ls = line.strip().split('=')
                if len(ls) == 2:
                    var, val = ls
                    vsr = var.split('_')
                    if(len(vsr) == 3 and vsr[0] == 'Property' and vsr[2] == 'Name'):
                        vsl = val.rsplit('.', 1)
                        if vsl[-1] == 'File':
                            is_file_property = True
                            continue
                if is_file_property:
                    job_id, param = ls[1].split('/', 1)
                    if job_id in job_url_list:
                        uri = job_url_list[job_id]
                        if uri not in param_list:
                            param_list[uri] = []
                        param_list[uri].append(param)
                is_file_property = False

        for uri, params in param_list.items():
            param_proto = None

            param_fn = None
            if uri[0:5].lower() == 's3://':
                uri_header, uri_body = uri.split('://', 1)
                us = uri_body.split('/', 1)
                bucketname = us.pop(0)
                base_key = us[0]
                logger.info(
                    'Creating session for S3 bucket {}'.format(bucketname))
                import boto3
                bucket = boto3.session.Session().resource('s3').Bucket(bucketname)
                paramlist = []
                for obj in bucket.objects.filter(Prefix=base_key):
                    fn = obj.key[len(base_key) + 1:]
                    if len(fn) > 0:
                        paramlist.append(fn)
                p = get_best_param(paramlist)
                if p is not None:
                    param_fn = uri + '/' + p
                    tempdir = tempfile.mkdtemp()
                    tmp = os.path.join(tempdir, p)
                    with open(tmp, 'wb') as f:
                        f.write(bucket.Object(
                            base_key + '/' + p).get()['Body'].read())
                    param_proto = load_parameters(tmp, proto_only=True)
                    rmtree(tempdir, ignore_errors=True)

            else:
                paramlist = []
                for fn in glob.glob('{}/*'.format(uri)):
                    paramlist.append(os.path.basename(fn))
                p = get_best_param(paramlist)
                if p is not None:
                    param_fn = os.path.join(uri, p)
                    param_proto = load_parameters(param_fn, proto_only=True)

            if param_proto is not None:
                for param in param_proto.parameter:
                    pn = param.variable_name.replace('/', '~')
                    if pn in params:
                        logger.log(99, 'Update variable {} from {}'.format(
                            param.variable_name, param_fn))
                        var = get_parameter_or_create(
                            param.variable_name, param.shape.dim)
                        var.d = np.reshape(param.data, param.shape.dim)
                        var.need_grad = param.need_grad
    if args.sdcproj:
        with open(args.sdcproj) as f:
            for line in f.readlines():
                ls = line.strip().split('=')
                if len(ls) == 2:
                    var, val = ls
                    if var == 'TimeLimit' and val:
                        timelimits = [int(x) for x in val.split(':')]
                        if len(timelimits) == 4:
                            config.timelimit = float(timelimits[0] * 24 * 3600 +
                                                     timelimits[1] * 3600 +
                                                     timelimits[2] * 60 + timelimits[3])

    config.global_config = info.global_config
    config.training_config = info.training_config

    comm = C.CurrentCommunicator()
    if not comm or comm.rank == 0:
        logger.log(99, 'Train with contexts {}'.format(available_contexts))

    class OptConfig:
        pass
    config.optimizers = OrderedDict()
    for name, opt in info.optimizers.items():
        o = OptConfig()
        o.optimizer = opt
        o.data_iterator = None
        config.optimizers[name] = o

    class MonConfig:
        pass
    config.monitors = OrderedDict()
    for name, mon in info.monitors.items():
        m = MonConfig()
        m.monitor = mon
        m.data_iterator = None
        config.monitors[name] = m

    # Training
    config.training_config.iter_per_epoch //= comm.size if comm else 1
    max_iter = config.training_config.max_epoch * \
        config.training_config.iter_per_epoch

    global _save_parameter_info
    _save_parameter_info = {}
    _, config_ext = os.path.splitext(args.config)
    if config_ext == '.prototxt' or config_ext == '.nntxt':
        _save_parameter_info['config'] = args.config
    elif config_ext == '.nnp':
        with zipfile.ZipFile(args.config, 'r') as nnp:
            for name in nnp.namelist():
                _, ext = os.path.splitext(name)
                if ext == '.nntxt' or ext == '.prototxt':
                    nnp.extract(name, args.outdir)
                    _save_parameter_info['config'] = os.path.join(
                        args.outdir, name)

    result = False
    if max_iter > 0:
        data_iterators = {'optimizer': {}, 'monitor': {}}
        with ExitStack() as stack:
            for name, o in config.optimizers.items():
                o.data_iterator = stack.enter_context(
                    o.optimizer.data_iterator())
                if comm and comm.size > 1:
                    o.data_iterator = o.data_iterator.slice(
                        comm.size, comm.rank)
            for name, m in config.monitors.items():
                m.data_iterator = stack.enter_context(
                    m.monitor.data_iterator())
                if comm and comm.size > 1:
                    m.data_iterator = m.data_iterator.slice(
                        comm.size, comm.rank)
            result = train(args, config)
    else:
        # save parameters without training (0 epoch learning)
        logger.log(99, '0 epoch learning. (Just save parameter.)')
        if not comm or comm.rank == 0:
            _save_parameters(args, 'current', 0, True)
        result = True

    if not comm or comm.rank == 0:
        if result:
            logger.log(99, 'Training Completed.')
        else:
            logger.log(99, 'Training Incompleted.')
    progress(None)
