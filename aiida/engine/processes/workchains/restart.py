# -*- coding: utf-8 -*-
###########################################################################
# Copyright (c), The AiiDA team. All rights reserved.                     #
# This file is part of the AiiDA code.                                    #
#                                                                         #
# The code is hosted on GitHub at https://github.com/aiidateam/aiida-core #
# For further information on the license, see the LICENSE.txt file        #
# For further information please visit http://www.aiida.net               #
###########################################################################
"""Base implementation of `WorkChain` class that implements a simple automated restart mechanism for sub processes."""
import functools

from aiida import orm
from aiida.common import AttributeDict

from .context import ToContext, append_
from .workchain import WorkChain
from .utils import ProcessHandlerReport, process_handler

__all__ = ('BaseRestartWorkChain',)


def validate_handler_overrides(process_class, handler_overrides, ctx):  # pylint: disable=inconsistent-return-statements,unused-argument
    """Validator for the `handler_overrides` input port of the `BaseRestartWorkChain.

    The `handler_overrides` should be a dictionary where keys are strings that are the name of a process handler, i.e. a
    instance method of the `process_class` that has been decorated with the `process_handler` decorator. The values
    should be boolean.

    .. note:: the normal signature of a port validator is `(value, ctx)` but since for the validation here we need a
        reference to the process class, we add it and the class is bound to the method in the port declaration in the
        `define` method.

    :param process_class: the `BaseRestartWorkChain` (sub) class
    :param handler_overrides: the input `Dict` node
    :param ctx: the `PortNamespace` in which the port is embedded
    """
    if not handler_overrides:
        return

    for handler, override in handler_overrides.get_dict().items():
        if not isinstance(handler, str):
            return 'The key `{}` is not a string.'.format(handler)

        if not process_class.is_process_handler(handler):
            return 'The key `{}` is not a process handler of {}'.format(handler, process_class)

        if not isinstance(override, bool):
            return 'The value of key `{}` is not a boolean.'.format(handler)


class BaseRestartWorkChain(WorkChain):
    """Base restart work chain.

    This work chain serves as the starting point for more complex work chains that will be designed to run a sub process
    that might need multiple restarts to come to a successful end. These restarts may be necessary because a single
    process run is not sufficient to achieve a fully converged result, or certain errors maybe encountered which
    are recoverable.

    This work chain implements the most basic functionality to achieve this goal. It will launch the sub process,
    restarting until it is completed successfully or the maximum number of iterations is reached. After completion of
    the sub process it will be inspected, and a list of process handlers are called successively. These process handlers
    are defined as class methods that are decorated with :meth:`~aiida.engine.process_handler`.

    The idea is to sub class this work chain and leverage the generic error handling that is implemented in the few
    outline methods. The minimally required outline would look something like the following::

        cls.setup
        while_(cls.should_run_process)(
            cls.run_process,
            cls.inspect_process,
        )

    Each of these methods can of course be overriden but they should be general enough to fit most process cycles. The
    `run_process` method will take the inputs for the process from the context under the key `inputs`. The user should,
    therefore, make sure that before the `run_process` method is called, that the to be used inputs are stored under
    `self.ctx.inputs`. One can update the inputs based on the results from a prior process by calling an outline method
    just before the `run_process` step, for example::

        cls.setup
        while_(cls.should_run_process)(
            cls.prepare_inputs,
            cls.run_process,
            cls.inspect_process,
        )

    Where in the `prepare_calculation` method, the inputs dictionary at `self.ctx.inputs` is updated before the next
    process will be run with those inputs.

    The `_process_class` attribute should be set to the `Process` class that should be run in the loop.
    Finally, to define handlers that will be called during the `inspect_process` simply define a class method with the
    signature `(self, node)` and decorate it with the `process_handler` decorator, for example::

        @process_handler
        def handle_problem(self, node):
            if some_problem:
                self.ctx.inputs = improved_inputs
                return ProcessHandlerReport()

    The `process_handler` and `ProcessHandlerReport` support various arguments to control the flow of the logic of the
    `inspect_process`. Refer to their respective documentation for details.
    """

    _process_class = None
    _considered_handlers_extra = 'considered_handlers'

    @classmethod
    def define(cls, spec):
        """Define the process specification."""
        # yapf: disable
        super().define(spec)
        spec.input('max_iterations', valid_type=orm.Int, default=lambda: orm.Int(5),
            help='Maximum number of iterations the work chain will restart the process to finish successfully.')
        spec.input('clean_workdir', valid_type=orm.Bool, default=lambda: orm.Bool(False),
            help='If `True`, work directories of all called calculation jobs will be cleaned at the end of execution.')
        spec.input('handler_overrides',
            valid_type=orm.Dict, required=False, validator=functools.partial(validate_handler_overrides, cls),
            help='Mapping where keys are process handler names and the values are a boolean, where `True` will enable '
                 'the corresponding handler and `False` will disable it. This overrides the default value set by the '
                 '`enabled` keyword of the `process_handler` decorator with which the method is decorated.')
        spec.exit_code(301, 'ERROR_SUB_PROCESS_EXCEPTED',
            message='The sub process excepted.')
        spec.exit_code(302, 'ERROR_SUB_PROCESS_KILLED',
            message='The sub process was killed.')
        spec.exit_code(401, 'ERROR_MAXIMUM_ITERATIONS_EXCEEDED',
            message='The maximum number of iterations was exceeded.')
        spec.exit_code(402, 'ERROR_SECOND_CONSECUTIVE_UNHANDLED_FAILURE',
            message='The process failed for an unknown reason, twice in a row.')

    def setup(self):
        """Initialize context variables that are used during the logical flow of the `BaseRestartWorkChain`."""
        overrides = self.inputs.handler_overrides.get_dict() if 'handler_overrides' in self.inputs else {}
        self.ctx.handler_overrides = overrides
        self.ctx.process_name = self._process_class.__name__
        self.ctx.unhandled_failure = False
        self.ctx.is_finished = False
        self.ctx.iteration = 0

    def should_run_process(self):
        """Return whether a new process should be run.

        This is the case as long as the last process has not finished successfully and the maximum number of restarts
        has not yet been exceeded.
        """
        return not self.ctx.is_finished and self.ctx.iteration < self.inputs.max_iterations.value

    def run_process(self):
        """Run the next process, taking the input dictionary from the context at `self.ctx.inputs`."""
        self.ctx.iteration += 1

        try:
            unwrapped_inputs = self.ctx.inputs
        except AttributeError:
            raise AttributeError('no process input dictionary was defined in `self.ctx.inputs`')

        # Set the `CALL` link label
        unwrapped_inputs['metadata']['call_link_label'] = 'iteration_{:02d}'.format(self.ctx.iteration)

        inputs = self._wrap_bare_dict_inputs(self._process_class.spec().inputs, unwrapped_inputs)
        node = self.submit(self._process_class, **inputs)

        # Add a new empty list to the `BaseRestartWorkChain._considered_handlers_extra` extra. This will contain the
        # name and return value of all class methods, decorated with `process_handler`, that are called during
        # the `inspect_process` outline step.
        considered_handlers = self.node.get_extra(self._considered_handlers_extra, [])
        considered_handlers.append([])
        self.node.set_extra(self._considered_handlers_extra, considered_handlers)

        self.report('launching {}<{}> iteration #{}'.format(self.ctx.process_name, node.pk, self.ctx.iteration))

        return ToContext(children=append_(node))

    def inspect_process(self):  # pylint: disable=inconsistent-return-statements,too-many-branches
        """Analyse the results of the previous process and call the handlers when necessary.

        If the process is excepted or killed, the work chain will abort. Otherwise any attached handlers will be called
        in order of their specified priority. If the process was failed and no handler returns a report indicating that
        the error was handled, it is considered an unhandled process failure and the process is relaunched. If this
        happens twice in a row, the work chain is aborted. In the case that at least one handler returned a report the
        following matrix determines the logic that is followed:

            Process  Handler    Handler     Action
            result   report?    exit code
            -----------------------------------------
            Success      yes        == 0     Restart
            Success      yes        != 0     Abort
            Failed       yes        == 0     Restart
            Failed       yes        != 0     Abort

        If no handler returned a report and the process finished successfully, the work chain's work is considered done
        and it will move on to the next step that directly follows the `while` conditional, if there is one defined in
        the outline.
        """
        node = self.ctx.children[self.ctx.iteration - 1]

        if node.is_excepted:
            return self.exit_codes.ERROR_SUB_PROCESS_EXCEPTED  # pylint: disable=no-member

        if node.is_killed:
            return self.exit_codes.ERROR_SUB_PROCESS_KILLED  # pylint: disable=no-member

        last_report = None

        # Sort the handlers with a priority defined, based on their priority in reverse order
        for handler in sorted(self.get_process_handlers(), key=lambda handler: handler.priority, reverse=True):

            # Skip if the handler is enabled, either explicitly through `handler_overrides` or by default
            if not self.ctx.handler_overrides.get(handler.__name__, handler.enabled):
                continue

            # Even though the `handler` is an instance method, the `get_process_handlers` method returns unbound methods
            # so we have to pass in `self` manually. Also, always pass the `node` as an argument because the
            # `process_handler` decorator with which the handler is decorated relies on this behavior.
            report = handler(self, node)

            if report is not None and not isinstance(report, ProcessHandlerReport):
                name = handler.__name__
                raise RuntimeError('handler `{}` returned a value that is not a ProcessHandlerReport'.format(name))

            # If an actual report was returned, save it so it is not overridden by next handler returning `None`
            if report:
                last_report = report

            # After certain handlers, we may want to skip all other handlers
            if report and report.do_break:
                break

        report_args = (self.ctx.process_name, node.pk)

        # If the process failed and no handler returned a report we consider it an unhandled failure
        if node.is_failed and not last_report:
            if self.ctx.unhandled_failure:
                template = '{}<{}> failed and error was not handled for the second consecutive time, aborting'
                self.report(template.format(*report_args))
                return self.exit_codes.ERROR_SECOND_CONSECUTIVE_UNHANDLED_FAILURE  # pylint: disable=no-member

            self.ctx.unhandled_failure = True
            self.report('{}<{}> failed and error was not handled, restarting once more'.format(*report_args))
            return

        # Here either the process finished successful or at least one handler returned a report so it can no longer be
        # considered to be an unhandled failed process and therefore we reset the flag
        self.ctx.unhandled_failure = True

        # If at least one handler returned a report, the action depends on its exit code and that of the process itself
        if last_report:
            if node.is_finished_ok and last_report.exit_code.status == 0:
                template = '{}<{}> finished successfully but a handler was triggered, restarting'
            elif node.is_failed and last_report.exit_code.status == 0:
                template = '{}<{}> failed but a handler dealt with the problem, restarting'
            elif node.is_finished_ok and last_report.exit_code.status != 0:
                template = '{}<{}> finished successfully but a handler detected an unrecoverable problem, aborting'
            elif node.is_failed and last_report.exit_code.status != 0:
                template = '{}<{}> failed but a handler detected an unrecoverable problem, aborting'

            self.report(template.format(*report_args))

            return report.exit_code

        # Otherwise the process was successful and no handler returned anything so we consider the work done
        self.ctx.is_finished = True

    def results(self):  # pylint: disable=inconsistent-return-statements
        """Attach the outputs specified in the output specification from the last completed process."""
        node = self.ctx.children[self.ctx.iteration - 1]

        # We check the `is_finished` attribute of the work chain and not the successfulness of the last process
        # because the error handlers in the last iteration can have qualified a "failed" process as satisfactory
        # for the outcome of the work chain and so have marked it as `is_finished=True`.
        if not self.ctx.is_finished and self.ctx.iteration >= self.inputs.max_iterations.value:
            self.report('reached the maximum number of iterations {}: last ran {}<{}>'.format(
                self.inputs.max_iterations.value, self.ctx.process_name, node.pk))
            return self.exit_codes.ERROR_MAXIMUM_ITERATIONS_EXCEEDED  # pylint: disable=no-member

        self.report('work chain completed after {} iterations'.format(self.ctx.iteration))

        for name, port in self.spec().outputs.items():

            try:
                output = node.get_outgoing(link_label_filter=name).one().node
            except ValueError:
                if port.required:
                    self.report("required output '{}' was not an output of {}<{}>".format(
                        name, self.ctx.process_name, node.pk))
            else:
                self.out(name, output)

    def __init__(self, *args, **kwargs):
        """Construct the instance."""
        from ..process import Process  # pylint: disable=cyclic-import
        super().__init__(*args, **kwargs)

        if self._process_class is None or not issubclass(self._process_class, Process):
            raise ValueError('no valid Process class defined for `_process_class` attribute')

    @classmethod
    def is_process_handler(cls, process_handler_name):
        """Return whether the given method name corresponds to a process handler of this class.

        :param process_handler_name: string name of the instance method
        :return: boolean, True if corresponds to process handler, False otherwise
        """
        # pylint: disable=comparison-with-callable
        if isinstance(process_handler_name, str):
            handler = getattr(cls, process_handler_name, {})
        else:
            handler = process_handler_name

        return getattr(handler, 'decorator', None) == process_handler

    @classmethod
    def get_process_handlers(cls):
        from inspect import getmembers
        return [method[1] for method in getmembers(cls) if cls.is_process_handler(method[1])]

    def on_terminated(self):
        """Clean the working directories of all child calculation jobs if `clean_workdir=True` in the inputs."""
        super().on_terminated()

        if self.inputs.clean_workdir.value is False:
            self.report('remote folders will not be cleaned')
            return

        cleaned_calcs = []

        for called_descendant in self.node.called_descendants:
            if isinstance(called_descendant, orm.CalcJobNode):
                try:
                    called_descendant.outputs.remote_folder._clean()  # pylint: disable=protected-access
                    cleaned_calcs.append(str(called_descendant.pk))
                except (IOError, OSError, KeyError):
                    pass

        if cleaned_calcs:
            self.report('cleaned remote folders of calculations: {}'.format(' '.join(cleaned_calcs)))

    def _wrap_bare_dict_inputs(self, port_namespace, inputs):
        """Wrap bare dictionaries in `inputs` in a `Dict` node if dictated by the corresponding inputs portnamespace.

        :param port_namespace: a `PortNamespace`
        :param inputs: a dictionary of inputs intended for submission of the process
        :return: an attribute dictionary with all bare dictionaries wrapped in `Dict` if dictated by the port namespace
        """
        from aiida.engine.processes import PortNamespace

        wrapped = {}

        for key, value in inputs.items():

            if key not in port_namespace:
                wrapped[key] = value
                continue

            port = port_namespace[key]

            if isinstance(port, PortNamespace):
                wrapped[key] = self._wrap_bare_dict_inputs(port, value)
            elif port.valid_type == orm.Dict and isinstance(value, dict):
                wrapped[key] = orm.Dict(dict=value)
            else:
                wrapped[key] = value

        return AttributeDict(wrapped)
