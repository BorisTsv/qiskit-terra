# This code is part of Qiskit.
#
# (C) Copyright IBM 2023.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Internal format of calibration data in target."""
import inspect
from abc import ABCMeta, abstractmethod
from enum import IntEnum
from typing import Callable, List, Union, Optional, Sequence, Any

from qiskit.pulse.exceptions import PulseError
from qiskit.pulse.schedule import Schedule, ScheduleBlock
from qiskit.qobj.converters import QobjToInstructionConverter
from qiskit.qobj.pulse_qobj import PulseQobjInstruction


class CalibrationPublisher(IntEnum):
    """Defines who defined schedule entry."""

    BACKEND_PROVIDER = 0
    QISKIT = 1
    EXPERIMENT_SERVICE = 2


class CalibrationEntry(metaclass=ABCMeta):
    """A metaclass of a calibration entry."""

    @abstractmethod
    def define(self, definition: Any):
        """Attach definition to the calibration entry.

        Args:
            definition: Definition of this entry.
        """
        pass

    @abstractmethod
    def get_signature(self) -> inspect.Signature:
        """Return signature object associated with entry definition.

        Returns:
            Signature object.
        """
        pass

    @abstractmethod
    def get_schedule(self, *args, **kwargs) -> Union[Schedule, ScheduleBlock]:
        """Generate schedule from entry definition.

        Args:
            args: Command parameters.
            kwargs: Command keyword parameters.

        Returns:
            Pulse schedule with assigned parameters.
        """
        pass


class ScheduleDef(CalibrationEntry):
    """In-memory Qiskit Pulse representation.

    A pulse schedule must provide signature with the .parameters attribute.
    This entry can be parameterized by a Qiskit Parameter object.
    The .get_schedule method returns a parameter-assigned pulse program.
    """

    def __init__(self, arguments: Optional[Sequence[str]] = None):
        """Define an empty entry.

        Args:
            arguments: User provided argument names for this entry, if parameterized.

        Raises:
            PulseError: When `arguments` is not a sequence of string.
        """
        if arguments and not all(isinstance(arg, str) for arg in arguments):
            raise PulseError(f"Arguments must be name of parameters. Not {arguments}.")
        if arguments:
            arguments = list(arguments)
        self._user_arguments = arguments

        self._definition = None
        self._signature = None

    def _parse_argument(self):
        """Generate signature from program and user provided argument names."""
        # This doesn't assume multiple parameters with the same name
        # Parameters with the same name are treated identically
        all_argnames = set(map(lambda x: x.name, self._definition.parameters))

        if self._user_arguments:
            if set(self._user_arguments) != all_argnames:
                raise PulseError(
                    "Specified arguments don't match with schedule parameters. "
                    f"{self._user_arguments} != {self._definition.parameters}."
                )
            argnames = list(self._user_arguments)
        else:
            argnames = sorted(all_argnames)

        params = []
        for argname in argnames:
            param = inspect.Parameter(
                argname,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
            params.append(param)
        signature = inspect.Signature(
            parameters=params,
            return_annotation=type(self._definition),
        )
        self._signature = signature

    def define(self, definition: Union[Schedule, ScheduleBlock]):
        self._definition = definition
        # add metadata
        if "publisher" not in definition.metadata:
            definition.metadata["publisher"] = CalibrationPublisher.QISKIT
        self._parse_argument()

    def get_signature(self) -> inspect.Signature:
        return self._signature

    def get_schedule(self, *args, **kwargs) -> Union[Schedule, ScheduleBlock]:
        if not args and not kwargs:
            return self._definition
        try:
            to_bind = self.get_signature().bind_partial(*args, **kwargs)
        except TypeError as ex:
            raise PulseError("Assigned parameter doesn't match with schedule parameters.") from ex
        value_dict = {}
        for param in self._definition.parameters:
            # Schedule allows partial bind. This results in parameterized Schedule.
            try:
                value_dict[param] = to_bind.arguments[param.name]
            except KeyError:
                pass
        return self._definition.assign_parameters(value_dict, inplace=False)

    def __eq__(self, other):
        # This delegates equality check to Schedule or ScheduleBlock.
        return self._definition == other._definition

    def __str__(self):
        out = f"Schedule {self._definition.name}"
        params_str = ", ".join(self.get_signature().parameters.keys())
        if params_str:
            out += f"({params_str})"
        return out


class CallableDef(CalibrationEntry):
    """Python callback function that generates Qiskit Pulse program.

    A callable is inspected by the python built-in inspection module and
    provide the signature. This entry is parameterized by the function signature
    and .get_schedule method returns a non-parameterized pulse program
    by consuming the provided arguments and keyword arguments.
    """

    def __init__(self):
        """Define an empty entry."""
        self._definition = None
        self._signature = None

    def define(self, definition: Callable):
        self._definition = definition
        self._signature = inspect.signature(definition)

    def get_signature(self) -> inspect.Signature:
        return self._signature

    def get_schedule(self, *args, **kwargs) -> Union[Schedule, ScheduleBlock]:
        try:
            # Python function doesn't allow partial bind, but default value can exist.
            to_bind = self._signature.bind(*args, **kwargs)
            to_bind.apply_defaults()
        except TypeError as ex:
            raise PulseError("Assigned parameter doesn't match with function signature.") from ex

        schedule = self._definition(**to_bind.arguments)
        # add metadata
        if "publisher" not in schedule.metadata:
            schedule.metadata["publisher"] = CalibrationPublisher.QISKIT
        return schedule

    def __eq__(self, other):
        # We cannot evaluate function equality without parsing python AST.
        # This simply compares wether they are the same object.
        return self._definition is other._definition

    def __str__(self):
        params_str = ", ".join(self.get_signature().parameters.keys())
        return f"Callable {self._definition.__name__}({params_str})"


class PulseQobjDef(ScheduleDef):
    """Qobj JSON serialized format instruction sequence.

    A JSON serialized program can be converted into Qiskit Pulse program with
    the provided qobj converter. Because the Qobj JSON doesn't provide signature,
    conversion process occurs when the signature is requested for the first time
    and the generated pulse program is cached for performance.
    """

    def __init__(
        self,
        arguments: Optional[Sequence[str]] = None,
        converter: Optional[QobjToInstructionConverter] = None,
        name: Optional[str] = None,
    ):
        """Define an empty entry.

        Args:
            arguments: User provided argument names for this entry, if parameterized.
            converter: Optional. Qobj to Qiskit converter.
            name: Name of schedule.
        """
        super().__init__(arguments=arguments)

        self._converter = converter or QobjToInstructionConverter(pulse_library=[])
        self._name = name
        self._source = None

    def _build_schedule(self):
        """Build pulse schedule from cmd-def sequence."""
        schedule = Schedule(name=self._name)
        for qobj_inst in self._source:
            for qiskit_inst in self._converter._get_sequences(qobj_inst):
                schedule.insert(qobj_inst.t0, qiskit_inst, inplace=True)
        schedule.metadata["publisher"] = CalibrationPublisher.BACKEND_PROVIDER

        self._definition = schedule
        self._parse_argument()

    def define(self, definition: List[PulseQobjInstruction]):
        # This doesn't generate signature immediately, because of lazy schedule build.
        self._source = definition

    def get_signature(self) -> inspect.Signature:
        if self._definition is None:
            self._build_schedule()
        return super().get_signature()

    def get_schedule(self, *args, **kwargs) -> Union[Schedule, ScheduleBlock]:
        if self._definition is None:
            self._build_schedule()
        return super().get_schedule(*args, **kwargs)

    def __eq__(self, other):
        if isinstance(other, PulseQobjDef):
            # If both objects are Qobj just check Qobj equality.
            return self._source == other._source
        if isinstance(other, ScheduleDef) and self._definition is None:
            # To compare with other scheudle def, this also generates schedule object from qobj.
            self._build_schedule()
        return self._definition == other._definition

    def __str__(self):
        if self._definition is None:
            # Avoid parsing schedule for pretty print.
            return "PulseQobj"
        return super().__str__()
