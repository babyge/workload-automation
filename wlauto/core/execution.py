#    Copyright 2013-2015 ARM Limited
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
#

# pylint: disable=no-member

"""
This module contains the execution logic for Workload Automation. It defines the
following actors:

    WorkloadSpec: Identifies the workload to be run and defines parameters under
                  which it should be executed.

    Executor: Responsible for the overall execution process. It instantiates
              and/or intialises the other actors, does any necessary vaidation
              and kicks off the whole process.

    Execution Context: Provides information about the current state of run
                       execution to instrumentation.

    RunInfo: Information about the current run.

    Runner: This executes workload specs that are passed to it. It goes through
            stages of execution, emitting an appropriate signal at each step to
            allow instrumentation to do its stuff.

"""
import logging
import os
import random
import subprocess
import uuid
from collections import Counter, defaultdict, OrderedDict
from contextlib import contextmanager
from copy import copy
from datetime import datetime
from itertools import izip_longest

import wlauto.core.signal as signal
from wlauto.core import instrumentation
from wlauto.core import pluginloader
from wlauto.core.configuration import settings
from wlauto.core.device_manager import TargetInfo
from wlauto.core.plugin import Artifact
from wlauto.core.resolver import ResourceResolver
from wlauto.core.result import ResultManager, IterationResult, RunResult
from wlauto.exceptions import (WAError, ConfigError, TimeoutError, InstrumentError,
                               DeviceError, DeviceNotRespondingError)
from wlauto.utils.misc import (ensure_directory_exists as _d, 
                               get_traceback, format_duration)
from wlauto.utils.serializer import json


# The maximum number of reboot attempts for an iteration.
MAX_REBOOT_ATTEMPTS = 3

# If something went wrong during device initialization, wait this
# long (in seconds) before retrying. This is necessary, as retrying
# immediately may not give the device enough time to recover to be able
# to reboot.
REBOOT_DELAY = 3


class ExecutionContext(object):
    """
    Provides a context for instrumentation. Keeps track of things like
    current workload and iteration.

    This class also provides two status members that can be used by workloads
    and instrumentation to keep track of arbitrary state. ``result``
    is reset on each new iteration of a workload; run_status is maintained
    throughout a Workload Automation run.

    """

    # These are the artifacts generated by the core framework.
    default_run_artifacts = [
        Artifact('runlog', 'run.log', 'log', mandatory=True,
                 description='The log for the entire run.'),
    ]

    @property
    def current_iteration(self):
        if self.current_job:
            spec_id = self.current_job.spec.id
            return self.job_iteration_counts[spec_id]
        else:
            return None

    @property
    def job_status(self):
        if not self.current_job:
            return None
        return self.current_job.result.status

    @property
    def workload(self):
        return getattr(self.spec, 'workload', None)

    @property
    def spec(self):
        return getattr(self.current_job, 'spec', None)

    @property
    def result(self):
        return getattr(self.current_job, 'result', self.run_result)

    def __init__(self, device_manager, config):
        self.device_manager = device_manager
        self.device = self.device_manager.target
        self.config = config
        self.reboot_policy = config.reboot_policy
        self.output_directory = None
        self.current_job = None
        self.resolver = None
        self.last_error = None
        self.run_info = None
        self.run_result = None
        self.run_output_directory = self.config.output_directory
        self.host_working_directory = self.config.meta_directory
        self.iteration_artifacts = None
        self.run_artifacts = copy(self.default_run_artifacts)
        self.job_iteration_counts = defaultdict(int)
        self.aborted = False
        self.runner = None
        if config.agenda.filepath:
            self.run_artifacts.append(Artifact('agenda',
                                               os.path.join(self.host_working_directory,
                                                            os.path.basename(config.agenda.filepath)),
                                               'meta',
                                               mandatory=True,
                                               description='Agenda for this run.'))
        for i, filepath in enumerate(settings.config_paths, 1):
            name = 'config_{}'.format(i)
            path = os.path.join(self.host_working_directory,
                                name + os.path.splitext(filepath)[1])
            self.run_artifacts.append(Artifact(name,
                                               path,
                                               kind='meta',
                                               mandatory=True,
                                               description='Config file used for the run.'))

    def initialize(self):
        if not os.path.isdir(self.run_output_directory):
            os.makedirs(self.run_output_directory)
        self.output_directory = self.run_output_directory
        self.resolver = ResourceResolver(self.config)
        self.run_info = RunInfo(self.config)
        self.run_result = RunResult(self.run_info, self.run_output_directory)

    def next_job(self, job):
        """Invoked by the runner when starting a new iteration of workload execution."""
        self.current_job = job
        self.job_iteration_counts[self.spec.id] += 1
        if not self.aborted:
            outdir_name = '_'.join(map(str, [self.spec.label, self.spec.id, self.current_iteration]))
            self.output_directory = _d(os.path.join(self.run_output_directory, outdir_name))
            self.iteration_artifacts = [wa for wa in self.workload.artifacts]
        self.current_job.result.iteration = self.current_iteration
        self.current_job.result.output_directory = self.output_directory

    def end_job(self):
        if self.current_job.result.status == IterationResult.ABORTED:
            self.aborted = True
        self.current_job = None
        self.output_directory = self.run_output_directory

    def add_metric(self, *args, **kwargs):
        self.result.add_metric(*args, **kwargs)

    def add_artifact(self, name, path, kind, *args, **kwargs):
        if self.current_job is None:
            self.add_run_artifact(name, path, kind, *args, **kwargs)
        else:
            self.add_iteration_artifact(name, path, kind, *args, **kwargs)

    def add_run_artifact(self, name, path, kind, *args, **kwargs):
        path = _check_artifact_path(path, self.run_output_directory)
        self.run_artifacts.append(Artifact(name, path, kind, Artifact.ITERATION, *args, **kwargs))

    def add_iteration_artifact(self, name, path, kind, *args, **kwargs):
        path = _check_artifact_path(path, self.output_directory)
        self.iteration_artifacts.append(Artifact(name, path, kind, Artifact.RUN, *args, **kwargs))

    def get_artifact(self, name):
        if self.iteration_artifacts:
            for art in self.iteration_artifacts:
                if art.name == name:
                    return art
        for art in self.run_artifacts:
            if art.name == name:
                return art
        return None


def _check_artifact_path(path, rootpath):
    if path.startswith(rootpath):
        return os.path.abspath(path)
    rootpath = os.path.abspath(rootpath)
    full_path = os.path.join(rootpath, path)
    if not os.path.isfile(full_path):
        raise ValueError('Cannot add artifact because {} does not exist.'.format(full_path))
    return full_path



class FakeTargetManager(object):

    def __init__(self, name, config):
        self.device_name = name
        self.device_config = config

        from devlib import LocalLinuxTarget
        self.target = LocalLinuxTarget({'unrooted': True})
        
    def get_target_info(self):
        return TargetInfo(self.target)

    def validate_runtime_parameters(self, params):
        pass

    def merge_runtime_parameters(self, params):
        pass


def init_target_manager(config):
    return FakeTargetManager(config.device, config.device_config)


class Executor(object):
    """
    The ``Executor``'s job is to set up the execution context and pass to a
    ``Runner`` along with a loaded run specification. Once the ``Runner`` has
    done its thing, the ``Executor`` performs some final reporint before
    returning.

    The initial context set up involves combining configuration from various
    sources, loading of requided workloads, loading and installation of
    instruments and result processors, etc. Static validation of the combined
    configuration is also performed.

    """
    # pylint: disable=R0915

    def __init__(self):
        self.logger = logging.getLogger('Executor')
        self.error_logged = False
        self.warning_logged = False
        pluginloader = None
        self.device_manager = None
        self.device = None
        self.context = None

    def execute(self, config_manager, output):
        """
        Execute the run specified by an agenda. Optionally, selectors may be
        used to only selecute a subset of the specified agenda.

        Params::

            :state: a ``ConfigManager`` containing processed configuraiton
            :output: an initialized ``RunOutput`` that will be used to
                     store the results.

        """
        signal.connect(self._error_signalled_callback, signal.ERROR_LOGGED)
        signal.connect(self._warning_signalled_callback, signal.WARNING_LOGGED)

        self.logger.info('Initializing run')
        self.logger.debug('Finalizing run configuration.')
        config = config_manager.finalize()
        output.write_config(config)

        self.logger.info('Connecting to target')
        target_manager = init_target_manager(config.run_config)
        output.write_target_info(target_manager.get_target_info())

        self.logger.info('Generationg jobs')
        job_specs = config_manager.jobs_config.generate_job_specs(target_manager)
        output.write_job_specs(job_specs)

    def old_exec(self, agenda, selectors={}):
        self.config.set_agenda(agenda, selectors)
        self.config.finalize()
        config_outfile = os.path.join(self.config.meta_directory, 'run_config.json')
        with open(config_outfile, 'w') as wfh:
            json.dump(self.config, wfh)

        self.logger.debug('Initialising device configuration.')
        if not self.config.device:
            raise ConfigError('Make sure a device is specified in the config.')
        self.device_manager = pluginloader.get_manager(self.config.device, 
                                                       **self.config.device_config)
        self.device_manager.validate()
        self.device = self.device_manager.target

        self.context = ExecutionContext(self.device_manager, self.config)

        self.logger.debug('Loading resource discoverers.')
        self.context.initialize()
        self.context.resolver.load()
        self.context.add_artifact('run_config', config_outfile, 'meta')

        self.logger.debug('Installing instrumentation')
        for name, params in self.config.instrumentation.iteritems():
            instrument = pluginloader.get_instrument(name, self.device, **params)
            instrumentation.install(instrument)
        instrumentation.validate()

        self.logger.debug('Installing result processors')
        result_manager = ResultManager()
        for name, params in self.config.result_processors.iteritems():
            processor = pluginloader.get_result_processor(name, **params)
            result_manager.install(processor)
        result_manager.validate()

        self.logger.debug('Loading workload specs')
        for workload_spec in self.config.workload_specs:
            workload_spec.load(self.device, pluginloader)
            workload_spec.workload.init_resources(self.context)
            workload_spec.workload.validate()

        if self.config.flashing_config:
            if not self.device.flasher:
                msg = 'flashing_config specified for {} device that does not support flashing.'
                raise ConfigError(msg.format(self.device.name))
            self.logger.debug('Flashing the device')
            self.device.flasher.flash(self.device)

        self.logger.info('Running workloads')
        runner = self._get_runner(result_manager)
        runner.init_queue(self.config.workload_specs)
        runner.run()
        self.execute_postamble()

    def execute_postamble(self):
        """
        This happens after the run has completed. The overall results of the run are
        summarised to the user.

        """
        result = self.context.run_result
        counter = Counter()
        for ir in result.iteration_results:
            counter[ir.status] += 1
        self.logger.info('Done.')
        self.logger.info('Run duration: {}'.format(format_duration(self.context.run_info.duration)))
        status_summary = 'Ran a total of {} iterations: '.format(sum(self.context.job_iteration_counts.values()))
        parts = []
        for status in IterationResult.values:
            if status in counter:
                parts.append('{} {}'.format(counter[status], status))
        self.logger.info(status_summary + ', '.join(parts))
        self.logger.info('Results can be found in {}'.format(self.config.output_directory))

        if self.error_logged:
            self.logger.warn('There were errors during execution.')
            self.logger.warn('Please see {}'.format(self.config.log_file))
        elif self.warning_logged:
            self.logger.warn('There were warnings during execution.')
            self.logger.warn('Please see {}'.format(self.config.log_file))

    def _get_runner(self, result_manager):
        if not self.config.execution_order or self.config.execution_order == 'by_iteration':
            if self.config.reboot_policy == 'each_spec':
                self.logger.info('each_spec reboot policy with the default by_iteration execution order is '
                                 'equivalent to each_iteration policy.')
            runnercls = ByIterationRunner
        elif self.config.execution_order in ['classic', 'by_spec']:
            runnercls = BySpecRunner
        elif self.config.execution_order == 'by_section':
            runnercls = BySectionRunner
        elif self.config.execution_order == 'random':
            runnercls = RandomRunner
        else:
            raise ConfigError('Unexpected execution order: {}'.format(self.config.execution_order))
        return runnercls(self.device_manager, self.context, result_manager)

    def _error_signalled_callback(self):
        self.error_logged = True
        signal.disconnect(self._error_signalled_callback, signal.ERROR_LOGGED)

    def _warning_signalled_callback(self):
        self.warning_logged = True
        signal.disconnect(self._warning_signalled_callback, signal.WARNING_LOGGED)


class RunnerJob(object):
    """
    Represents a single execution of a ``RunnerJobDescription``. There will be one created for each iteration
    specified by ``RunnerJobDescription.number_of_iterations``.

    """

    def __init__(self, spec, retry=0):
        self.spec = spec
        self.retry = retry
        self.iteration = None
        self.result = IterationResult(self.spec)


class Runner(object):
    """
    This class is responsible for actually performing a workload automation
    run. The main responsibility of this class is to emit appropriate signals
    at the various stages of the run to allow things like traces an other
    instrumentation to hook into the process.

    This is an abstract base class that defines each step of the run, but not
    the order in which those steps are executed, which is left to the concrete
    derived classes.

    """
    class _RunnerError(Exception):
        """Internal runner error."""
        pass

    @property
    def config(self):
        return self.context.config

    @property
    def current_job(self):
        if self.job_queue:
            return self.job_queue[0]
        return None

    @property
    def previous_job(self):
        if self.completed_jobs:
            return self.completed_jobs[-1]
        return None

    @property
    def next_job(self):
        if self.job_queue:
            if len(self.job_queue) > 1:
                return self.job_queue[1]
        return None

    @property
    def spec_changed(self):
        if self.previous_job is None and self.current_job is not None:  # Start of run
            return True
        if self.previous_job is not None and self.current_job is None:  # End of run
            return True
        return self.current_job.spec.id != self.previous_job.spec.id

    @property
    def spec_will_change(self):
        if self.current_job is None and self.next_job is not None:  # Start of run
            return True
        if self.current_job is not None and self.next_job is None:  # End of run
            return True
        return self.current_job.spec.id != self.next_job.spec.id

    def __init__(self, device_manager, context, result_manager):
        self.device_manager = device_manager
        self.device = device_manager.target
        self.context = context
        self.result_manager = result_manager
        self.logger = logging.getLogger('Runner')
        self.job_queue = []
        self.completed_jobs = []
        self._initial_reset = True

    def init_queue(self, specs):
        raise NotImplementedError()

    def run(self):  # pylint: disable=too-many-branches
        self._send(signal.RUN_START)
        self._initialize_run()

        try:
            while self.job_queue:
                try:
                    self._init_job()
                    self._run_job()
                except KeyboardInterrupt:
                    self.current_job.result.status = IterationResult.ABORTED
                    raise
                except Exception, e:  # pylint: disable=broad-except
                    self.current_job.result.status = IterationResult.FAILED
                    self.current_job.result.add_event(e.message)
                    if isinstance(e, DeviceNotRespondingError):
                        self.logger.info('Device appears to be unresponsive.')
                        if self.context.reboot_policy.can_reboot and self.device.can('reset_power'):
                            self.logger.info('Attempting to hard-reset the device...')
                            try:
                                self.device.boot(hard=True)
                                self.device.connect()
                            except DeviceError:  # hard_boot not implemented for the device.
                                raise e
                        else:
                            raise e
                    else:  # not a DeviceNotRespondingError
                        self.logger.error(e)
                finally:
                    self._finalize_job()
        except KeyboardInterrupt:
            self.logger.info('Got CTRL-C. Finalizing run... (CTRL-C again to abort).')
            # Skip through the remaining jobs.
            while self.job_queue:
                self.context.next_job(self.current_job)
                self.current_job.result.status = IterationResult.ABORTED
                self._finalize_job()
        except DeviceNotRespondingError:
            self.logger.info('Device unresponsive and recovery not possible. Skipping the rest of the run.')
            self.context.aborted = True
            while self.job_queue:
                self.context.next_job(self.current_job)
                self.current_job.result.status = IterationResult.SKIPPED
                self._finalize_job()

        instrumentation.enable_all()
        self._finalize_run()
        self._process_results()

        self.result_manager.finalize(self.context)
        self._send(signal.RUN_END)

    def _initialize_run(self):
        self.context.runner = self
        self.context.run_info.start_time = datetime.utcnow()
        self._connect_to_device()
        self.logger.info('Initializing device')
        self.device_manager.initialize(self.context)

        self.logger.info('Initializing workloads')
        for workload_spec in self.context.config.workload_specs:
            workload_spec.workload.initialize(self.context)

        self.context.run_info.device_properties = self.device_manager.info
        self.result_manager.initialize(self.context)
        self._send(signal.RUN_INIT)

        if instrumentation.check_failures():
            raise InstrumentError('Detected failure(s) during instrumentation initialization.')

    def _connect_to_device(self):
        if self.context.reboot_policy.perform_initial_boot:
            try:
                self.device_manager.connect()
            except DeviceError:  # device may be offline
                if self.device.can('reset_power'):
                    with self._signal_wrap('INITIAL_BOOT'):
                        self.device.boot(hard=True)
                else:
                    raise DeviceError('Cannot connect to device for initial reboot; '
                                      'and device does not support hard reset.')
            else:  # successfully connected
                self.logger.info('\tBooting device')
                with self._signal_wrap('INITIAL_BOOT'):
                    self._reboot_device()
        else:
            self.logger.info('Connecting to device')
            self.device_manager.connect()

    def _init_job(self):
        self.current_job.result.status = IterationResult.RUNNING
        self.context.next_job(self.current_job)

    def _run_job(self):   # pylint: disable=too-many-branches
        spec = self.current_job.spec
        if not spec.enabled:
            self.logger.info('Skipping workload %s (iteration %s)', spec, self.context.current_iteration)
            self.current_job.result.status = IterationResult.SKIPPED
            return

        self.logger.info('Running workload %s (iteration %s)', spec, self.context.current_iteration)
        if spec.flash:
            if not self.context.reboot_policy.can_reboot:
                raise ConfigError('Cannot flash as reboot_policy does not permit rebooting.')
            if not self.device.can('flash'):
                raise DeviceError('Device does not support flashing.')
            self._flash_device(spec.flash)
        elif not self.completed_jobs:
            # Never reboot on the very fist job of a run, as we would have done
            # the initial reboot if a reboot was needed.
            pass
        elif self.context.reboot_policy.reboot_on_each_spec and self.spec_changed:
            self.logger.debug('Rebooting on spec change.')
            self._reboot_device()
        elif self.context.reboot_policy.reboot_on_each_iteration:
            self.logger.debug('Rebooting on iteration.')
            self._reboot_device()

        instrumentation.disable_all()
        instrumentation.enable(spec.instrumentation)
        self.device_manager.start()

        if self.spec_changed:
            self._send(signal.WORKLOAD_SPEC_START)
        self._send(signal.ITERATION_START)

        try:
            setup_ok = False
            with self._handle_errors('Setting up device parameters'):
                self.device_manager.set_runtime_parameters(spec.runtime_parameters)
                setup_ok = True

            if setup_ok:
                with self._handle_errors('running {}'.format(spec.workload.name)):
                    self.current_job.result.status = IterationResult.RUNNING
                    self._run_workload_iteration(spec.workload)
            else:
                self.logger.info('\tSkipping the rest of the iterations for this spec.')
                spec.enabled = False
        except KeyboardInterrupt:
            self._send(signal.ITERATION_END)
            self._send(signal.WORKLOAD_SPEC_END)
            raise
        else:
            self._send(signal.ITERATION_END)
            if self.spec_will_change or not spec.enabled:
                self._send(signal.WORKLOAD_SPEC_END)
        finally:
            self.device_manager.stop()

    def _finalize_job(self):
        self.context.run_result.iteration_results.append(self.current_job.result)
        job = self.job_queue.pop(0)
        job.iteration = self.context.current_iteration
        if job.result.status in self.config.retry_on_status:
            if job.retry >= self.config.max_retries:
                self.logger.error('Exceeded maxium number of retries. Abandoning job.')
            else:
                self.logger.info('Job status was {}. Retrying...'.format(job.result.status))
                retry_job = RunnerJob(job.spec, job.retry + 1)
                self.job_queue.insert(0, retry_job)
        self.completed_jobs.append(job)
        self.context.end_job()

    def _finalize_run(self):
        self.logger.info('Finalizing workloads')
        for workload_spec in self.context.config.workload_specs:
            workload_spec.workload.finalize(self.context)

        self.logger.info('Finalizing.')
        self._send(signal.RUN_FIN)

        with self._handle_errors('Disconnecting from the device'):
            self.device.disconnect()

        info = self.context.run_info
        info.end_time = datetime.utcnow()
        info.duration = info.end_time - info.start_time

    def _process_results(self):
        self.logger.info('Processing overall results')
        with self._signal_wrap('OVERALL_RESULTS_PROCESSING'):
            if instrumentation.check_failures():
                self.context.run_result.non_iteration_errors = True
            self.result_manager.process_run_result(self.context.run_result, self.context)

    def _run_workload_iteration(self, workload):
        self.logger.info('\tSetting up')
        with self._signal_wrap('WORKLOAD_SETUP'):
            try:
                workload.setup(self.context)
            except:
                self.logger.info('\tSkipping the rest of the iterations for this spec.')
                self.current_job.spec.enabled = False
                raise
        try:

            self.logger.info('\tExecuting')
            with self._handle_errors('Running workload'):
                with self._signal_wrap('WORKLOAD_EXECUTION'):
                    workload.run(self.context)

            self.logger.info('\tProcessing result')
            self._send(signal.BEFORE_WORKLOAD_RESULT_UPDATE)
            try:
                if self.current_job.result.status != IterationResult.FAILED:
                    with self._handle_errors('Processing workload result',
                                             on_error_status=IterationResult.PARTIAL):
                        workload.update_result(self.context)
                        self._send(signal.SUCCESSFUL_WORKLOAD_RESULT_UPDATE)

                if self.current_job.result.status == IterationResult.RUNNING:
                    self.current_job.result.status = IterationResult.OK
            finally:
                self._send(signal.AFTER_WORKLOAD_RESULT_UPDATE)

        finally:
            self.logger.info('\tTearing down')
            with self._handle_errors('Tearing down workload',
                                     on_error_status=IterationResult.NONCRITICAL):
                with self._signal_wrap('WORKLOAD_TEARDOWN'):
                    workload.teardown(self.context)
            self.result_manager.add_result(self.current_job.result, self.context)

    def _flash_device(self, flashing_params):
        with self._signal_wrap('FLASHING'):
            self.device.flash(**flashing_params)
            self.device.connect()

    def _reboot_device(self):
        with self._signal_wrap('BOOT'):
            for reboot_attempts in xrange(MAX_REBOOT_ATTEMPTS):
                if reboot_attempts:
                    self.logger.info('\tRetrying...')
                with self._handle_errors('Rebooting device'):
                    self.device.boot(**self.current_job.spec.boot_parameters)
                    break
            else:
                raise DeviceError('Could not reboot device; max reboot attempts exceeded.')
            self.device.connect()

    def _send(self, s):
        signal.send(s, self, self.context)

    def _take_screenshot(self, filename):
        if self.context.output_directory:
            filepath = os.path.join(self.context.output_directory, filename)
        else:
            filepath = os.path.join(settings.output_directory, filename)
        self.device.capture_screen(filepath)

    @contextmanager
    def _handle_errors(self, action, on_error_status=IterationResult.FAILED):
        try:
            if action is not None:
                self.logger.debug(action)
            yield
        except (KeyboardInterrupt, DeviceNotRespondingError):
            raise
        except (WAError, TimeoutError), we:
            self.device.check_responsive()
            if self.current_job:
                self.current_job.result.status = on_error_status
                self.current_job.result.add_event(str(we))
            try:
                self._take_screenshot('error.png')
            except Exception, e:  # pylint: disable=W0703
                # We're already in error state, so the fact that taking a
                # screenshot failed is not surprising...
                pass
            if action:
                action = action[0].lower() + action[1:]
            self.logger.error('Error while {}:\n\t{}'.format(action, we))
        except Exception, e:  # pylint: disable=W0703
            error_text = '{}("{}")'.format(e.__class__.__name__, e)
            if self.current_job:
                self.current_job.result.status = on_error_status
                self.current_job.result.add_event(error_text)
            self.logger.error('Error while {}'.format(action))
            self.logger.error(error_text)
            if isinstance(e, subprocess.CalledProcessError):
                self.logger.error('Got:')
                self.logger.error(e.output)
            tb = get_traceback()
            self.logger.error(tb)

    @contextmanager
    def _signal_wrap(self, signal_name):
        """Wraps the suite in before/after signals, ensuring
        that after signal is always sent."""
        before_signal = getattr(signal, 'BEFORE_' + signal_name)
        success_signal = getattr(signal, 'SUCCESSFUL_' + signal_name)
        after_signal = getattr(signal, 'AFTER_' + signal_name)
        try:
            self._send(before_signal)
            yield
            self._send(success_signal)
        finally:
            self._send(after_signal)


class BySpecRunner(Runner):
    """
    This is that "classic" implementation that executes all iterations of a workload
    spec before proceeding onto the next spec.

    """

    def init_queue(self, specs):
        jobs = [[RunnerJob(s) for _ in xrange(s.number_of_iterations)] for s in specs]  # pylint: disable=unused-variable
        self.job_queue = [j for spec_jobs in jobs for j in spec_jobs]


class BySectionRunner(Runner):
    """
    Runs the first iteration for all benchmarks first, before proceeding to the next iteration,
    i.e. A1, B1, C1, A2, B2, C2...  instead of  A1, A1, B1, B2, C1, C2...

    If multiple sections where specified in the agenda, this will run all specs for the first section
    followed by all specs for the seciod section, etc.

    e.g. given sections X and Y, and global specs A and B, with 2 iterations, this will run

    X.A1, X.B1, Y.A1, Y.B1, X.A2, X.B2, Y.A2, Y.B2

    """

    def init_queue(self, specs):
        jobs = [[RunnerJob(s) for _ in xrange(s.number_of_iterations)] for s in specs]
        self.job_queue = [j for spec_jobs in izip_longest(*jobs) for j in spec_jobs if j]


class ByIterationRunner(Runner):
    """
    Runs the first iteration for all benchmarks first, before proceeding to the next iteration,
    i.e. A1, B1, C1, A2, B2, C2...  instead of  A1, A1, B1, B2, C1, C2...

    If multiple sections where specified in the agenda, this will run all sections for the first global
    spec first, followed by all sections for the second spec, etc.

    e.g. given sections X and Y, and global specs A and B, with 2 iterations, this will run

    X.A1, Y.A1, X.B1, Y.B1, X.A2, Y.A2, X.B2, Y.B2

    """

    def init_queue(self, specs):
        sections = OrderedDict()
        for s in specs:
            if s.section_id not in sections:
                sections[s.section_id] = []
            sections[s.section_id].append(s)
        specs = [s for section_specs in izip_longest(*sections.values()) for s in section_specs if s]
        jobs = [[RunnerJob(s) for _ in xrange(s.number_of_iterations)] for s in specs]
        self.job_queue = [j for spec_jobs in izip_longest(*jobs) for j in spec_jobs if j]


class RandomRunner(Runner):
    """
    This will run specs in a random order.

    """

    def init_queue(self, specs):
        jobs = [[RunnerJob(s) for _ in xrange(s.number_of_iterations)] for s in specs]  # pylint: disable=unused-variable
        all_jobs = [j for spec_jobs in jobs for j in spec_jobs]
        random.shuffle(all_jobs)
        self.job_queue = all_jobs
