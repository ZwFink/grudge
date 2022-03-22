"""
.. autoclass:: PyOpenCLArrayContext
.. autoclass:: PytatoPyOpenCLArrayContext
.. autoclass:: MPIBasedArrayContext
.. autoclass:: MPIPyOpenCLArrayContext
.. class:: MPIPytatoArrayContext
.. autofunction:: get_reasonable_array_context_class
"""

__copyright__ = "Copyright (C) 2020 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# {{{ imports

from typing import (
        TYPE_CHECKING, Mapping, Tuple, Any, Callable, Optional, Type)
from dataclasses import dataclass

from meshmode.array_context import (
        PyOpenCLArrayContext as _PyOpenCLArrayContextBase,
        PytatoPyOpenCLArrayContext as _PytatoPyOpenCLArrayContextBase)

import logging
logger = logging.getLogger(__name__)

try:
    # FIXME: temporary workaround while SingleGridWorkBalancingPytatoArrayContext
    # is not available in meshmode's main branch
    # (it currently needs
    # https://github.com/kaushikcfd/meshmode/tree/pytato-array-context-transforms)
    from meshmode.array_context import SingleGridWorkBalancingPytatoArrayContext

    try:
        # Crude check if we have the correct loopy branch
        # (https://github.com/kaushikcfd/loopy/tree/pytato-array-context-transforms)
        from loopy.codegen.result import get_idis_for_kernel  # noqa
    except ImportError:
        from warnings import warn
        warn("Your loopy and meshmode branches are mismatched. "
             "Please make sure that you have the "
             "https://github.com/kaushikcfd/loopy/tree/pytato-array-context-transforms "  # noqa
             "branch of loopy.")
        _HAVE_SINGLE_GRID_WORK_BALANCING = False
    else:
        _HAVE_SINGLE_GRID_WORK_BALANCING = True

except ImportError:
    _HAVE_SINGLE_GRID_WORK_BALANCING = False

from arraycontext.pytest import (
        _PytestPyOpenCLArrayContextFactoryWithClass,
        _PytestPytatoPyOpenCLArrayContextFactory,
        register_pytest_array_context_factory)
from arraycontext import ArrayContext
from arraycontext.container import ArrayContainer
from arraycontext.impl.pytato.compile import LazilyCompilingFunctionCaller

if TYPE_CHECKING:
    import pytato as pt
    from pytato.partition import PartId
    from pytato.distributed import DistributedGraphPartition
    import pyopencl
    import pyopencl.tools
    from mpi4py import MPI


class PyOpenCLArrayContext(_PyOpenCLArrayContextBase):
    """Inherits from :class:`meshmode.array_context.PyOpenCLArrayContext`. Extends it
    to understand :mod:`grudge`-specific transform metadata. (Of which there isn't
    any, for now.)
    """
    def __init__(self, queue: "pyopencl.CommandQueue",
            allocator: Optional["pyopencl.tools.AllocatorInterface"] = None,
            wait_event_queue_length: Optional[int] = None,
            force_device_scalars: bool = False) -> None:

        if allocator is None:
            from warnings import warn
            warn("No memory allocator specified, please pass one. "
                 "(Preferably a pyopencl.tools.MemoryPool in order "
                 "to reduce device allocations)")

        super().__init__(queue, allocator,
                         wait_event_queue_length, force_device_scalars)

# }}}


# {{{ pytato

class PytatoPyOpenCLArrayContext(_PytatoPyOpenCLArrayContextBase):
    """Inherits from :class:`meshmode.array_context.PytatoPyOpenCLArrayContext`.
    Extends it to understand :mod:`grudge`-specific transform metadata. (Of
    which there isn't any, for now.)
    """
    def __init__(self, queue, allocator=None):
        if allocator is None:
            from warnings import warn
            warn("No memory allocator specified, please pass one. "
                 "(Preferably a pyopencl.tools.MemoryPool in order "
                 "to reduce device allocations)")
        super().__init__(queue, allocator)

# }}}


class MPIBasedArrayContext:
    mpi_communicator: "MPI.Comm"


# {{{ distributed + pytato

class _DistributedLazilyCompilingFunctionCaller(LazilyCompilingFunctionCaller):
    def _dag_to_compiled_func(self, dict_of_named_arrays,
            input_id_to_name_in_program, output_id_to_name_in_program,
            output_template):

        from pytato.transform import deduplicate_data_wrappers
        dict_of_named_arrays = deduplicate_data_wrappers(dict_of_named_arrays)

        from pytato import find_distributed_partition
        distributed_partition = find_distributed_partition(dict_of_named_arrays)

        # {{{ turn symbolic tags into globally agreed-upon integers

        from pytato.distributed import number_distributed_tags
        prev_mpi_base_tag = self.actx.mpi_base_tag

        # type-ignore-reason: 'PytatoPyOpenCLArrayContext' has no 'mpi_communicator'
        # pylint: disable=no-member
        distributed_partition, _new_mpi_base_tag = number_distributed_tags(
                self.actx.mpi_communicator,
                distributed_partition,
                base_tag=prev_mpi_base_tag)

        assert prev_mpi_base_tag == self.actx.mpi_base_tag
        # FIXME: Updating stuff inside the array context from here is *cough*
        # not super pretty.
        self.actx.mpi_base_tag = _new_mpi_base_tag

        # }}}

        part_id_to_prg = {}

        from pytato import DictOfNamedArrays
        for part in distributed_partition.parts.values():
            d = DictOfNamedArrays(
                        {var_name: distributed_partition.var_name_to_result[var_name]
                            for var_name in part.output_names
                         })
            part_id_to_prg[part.pid], _, _ = self._dag_to_transformed_loopy_prg(d)

        return _DistributedCompiledFunction(
                actx=self.actx,
                distributed_partition=distributed_partition,
                part_id_to_prg=part_id_to_prg,
                input_id_to_name_in_program=input_id_to_name_in_program,
                output_id_to_name_in_program=output_id_to_name_in_program,
                output_template=output_template)


@dataclass(frozen=True)
class _DistributedCompiledFunction:
    """
    A callable which captures the :class:`pytato.target.BoundProgram`  resulting
    from calling :attr:`~LazilyCompilingFunctionCaller.f` with a given set of
    input types, and generating :mod:`loopy` IR from it.

    .. attribute:: pytato_program

    .. attribute:: input_id_to_name_in_program

        A mapping from input id to the placeholder name in
        :attr:`CompiledFunction.pytato_program`. Input id is represented as the
        position of :attr:`~LazilyCompilingFunctionCaller.f`'s argument augmented
        with the leaf array's key if the argument is an array container.

    .. attribute:: output_id_to_name_in_program

        A mapping from output id to the name of
        :class:`pytato.array.NamedArray` in
        :attr:`CompiledFunction.pytato_program`. Output id is represented by
        the key of a leaf array in the array container
        :attr:`CompiledFunction.output_template`.

    .. attribute:: output_template

       An instance of :class:`arraycontext.ArrayContainer` that is the return
       type of the callable.
    """

    actx: "MPISingleGridWorkBalancingPytatoArrayContext"
    distributed_partition: "DistributedGraphPartition"
    part_id_to_prg: "Mapping[PartId, pt.target.BoundProgram]"
    input_id_to_name_in_program: Mapping[Tuple[Any, ...], str]
    output_id_to_name_in_program: Mapping[Tuple[Any, ...], str]
    output_template: ArrayContainer

    def __call__(self, arg_id_to_arg) -> ArrayContainer:
        """
        :arg arg_id_to_arg: Mapping from input id to the passed argument. See
            :attr:`CompiledFunction.input_id_to_name_in_program` for input id's
            representation.
        """

        from arraycontext.impl.pytato.compile import _args_to_cl_buffers
        input_args_for_prg = _args_to_cl_buffers(
                self.actx, self.input_id_to_name_in_program, arg_id_to_arg)

        from pytato.distributed import execute_distributed_partition
        out_dict = execute_distributed_partition(
                self.distributed_partition, self.part_id_to_prg,
                self.actx.queue, self.actx.mpi_communicator,
                allocator=self.actx.allocator,
                input_args=input_args_for_prg)

        def to_output_template(keys, _):
            return self.actx.thaw(out_dict[self.output_id_to_name_in_program[keys]])

        from arraycontext.container.traversal import rec_keyed_map_array_container
        return rec_keyed_map_array_container(to_output_template,
                                             self.output_template)


class MPIPytatoArrayContextBase(MPIBasedArrayContext):
    def __init__(
            self, mpi_communicator, queue, *, mpi_base_tag, allocator=None
            ) -> None:
        if allocator is None:
            from warnings import warn
            warn("No memory allocator specified, please pass one. "
                 "(Preferably a pyopencl.tools.MemoryPool in order "
                 "to reduce device allocations)")

        super().__init__(queue, allocator)

        self.mpi_communicator = mpi_communicator
        self.mpi_base_tag = mpi_base_tag

    # FIXME: implement distributed-aware freeze

    def compile(self, f: Callable[..., Any]) -> Callable[..., Any]:
        return _DistributedLazilyCompilingFunctionCaller(self, f)

    def clone(self):
        # type-ignore-reason: 'DistributedLazyArrayContext' has no 'queue' member
        # pylint: disable=no-member
        return type(self)(self.mpi_communicator, self.queue,
                mpi_base_tag=self.mpi_base_tag,
                allocator=self.allocator)

# }}}


# {{{ distributed + pyopencl

class MPIPyOpenCLArrayContext(PyOpenCLArrayContext, MPIBasedArrayContext):
    """An array context for using distributed computation with :mod:`pyopencl`
    eager evaluation.

    .. autofunction:: __init__
    """

    def __init__(self,
            mpi_communicator,
            queue: "pyopencl.CommandQueue",
            *, allocator: Optional["pyopencl.tools.AllocatorInterface"] = None,
            wait_event_queue_length: Optional[int] = None,
            force_device_scalars: bool = False) -> None:
        """
        See :class:`arraycontext.impl.pyopencl.PyOpenCLArrayContext` for most
        arguments.
        """
        super().__init__(queue, allocator=allocator,
                wait_event_queue_length=wait_event_queue_length,
                force_device_scalars=force_device_scalars)

        self.mpi_communicator = mpi_communicator

    def clone(self):
        # type-ignore-reason: 'DistributedLazyArrayContext' has no 'queue' member
        # pylint: disable=no-member
        return type(self)(self.mpi_communicator, self.queue,
                allocator=self.allocator,
                wait_event_queue_length=self._wait_event_queue_length,
                force_device_scalars=self._force_device_scalars)

# }}}


# {{{ distributed + pytato array context subclasses

class MPIBasePytatoPyOpenCLArrayContext(
        MPIPytatoArrayContextBase, PytatoPyOpenCLArrayContext):
    """
    .. autofunction:: __init__
    """
    pass


if _HAVE_SINGLE_GRID_WORK_BALANCING:
    class MPISingleGridWorkBalancingPytatoArrayContext(
            MPIPytatoArrayContextBase, SingleGridWorkBalancingPytatoArrayContext):
        """
        .. autofunction:: __init__
        """

    MPIPytatoArrayContext = MPISingleGridWorkBalancingPytatoArrayContext
else:
    MPIPytatoArrayContext = MPIBasePytatoPyOpenCLArrayContext

# }}}


# {{{ pytest actx factory

class PytestPyOpenCLArrayContextFactory(
        _PytestPyOpenCLArrayContextFactoryWithClass):
    actx_class = PyOpenCLArrayContext


class PytestPytatoPyOpenCLArrayContextFactory(
        _PytestPytatoPyOpenCLArrayContextFactory):
    actx_class = PytatoPyOpenCLArrayContext


# deprecated
class PytestPyOpenCLArrayContextFactoryWithHostScalars(
        _PytestPyOpenCLArrayContextFactoryWithClass):
    actx_class = PyOpenCLArrayContext
    force_device_scalars = False


register_pytest_array_context_factory("grudge.pyopencl",
        PytestPyOpenCLArrayContextFactory)
register_pytest_array_context_factory("grudge.pytato-pyopencl",
        PytestPytatoPyOpenCLArrayContextFactory)

# }}}


# {{{ actx selection

def get_reasonable_array_context_class(
        lazy: bool = True, distributed: bool = True
        ) -> Type[ArrayContext]:
    """Returns a reasonable :class:`PyOpenCLArrayContext` currently
    supported given the constraints of *lazy* and *distributed*."""
    if lazy:
        if not _HAVE_SINGLE_GRID_WORK_BALANCING:
            from warnings import warn
            warn("No device-parallel actx available, execution will be slow. "
                 "Please make sure you have the right branches for loopy "
                 "(https://github.com/kaushikcfd/loopy/tree/pytato-array-context-transforms) "  # noqa
                 "and meshmode "
                 "(https://github.com/kaushikcfd/meshmode/tree/pytato-array-context-transforms).")  # noqa
        # lazy, non-distributed
        if not distributed:
            if _HAVE_SINGLE_GRID_WORK_BALANCING:
                actx_class = SingleGridWorkBalancingPytatoArrayContext
            else:
                actx_class = PytatoPyOpenCLArrayContext
        # distributed+lazy:
        if _HAVE_SINGLE_GRID_WORK_BALANCING:
            actx_class = MPISingleGridWorkBalancingPytatoArrayContext
        else:
            actx_class = MPIBasePytatoPyOpenCLArrayContext
    else:
        if distributed:
            actx_class = MPIPyOpenCLArrayContext
        else:
            actx_class = PyOpenCLArrayContext

    logger.info("get_reasonable_array_context_class: %s lazy=%r distributed=%r "
                "device-parallel=%r",
                actx_class.__name__, lazy, distributed,
                # eager is always device-parallel:
                (_HAVE_SINGLE_GRID_WORK_BALANCING or not lazy))
    return actx_class

# }}}


# vim: foldmethod=marker
