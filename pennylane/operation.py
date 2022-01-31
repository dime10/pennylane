# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# pylint: disable=protected-access
r"""
This module contains the abstract base classes for defining PennyLane
operations and observables.

Description
-----------

Qubit Operations
~~~~~~~~~~~~~~~~
The :class:`Operator` class serves as a base class for operators,
and is inherited by both the :class:`Observable` class and the
:class:`Operation` class. These classes are subclassed to implement quantum operations
and measure observables in PennyLane.

* Each :class:`~.Operator` subclass represents a general type of
  map between physical states. Each instance of these subclasses
  represents either

  - an application of the operator or
  - an instruction to measure and return the respective result.

  Operators act on a sequence of wires (subsystems) using given parameter values.

* Each :class:`~.Operation` subclass represents a type of quantum operation,
  for example a unitary quantum gate. Each instance of these subclasses
  represents an application of the operation with given parameter values to
  a given sequence of wires (subsystems).

* Each  :class:`~.Observable` subclass represents a type of physical observable.
  Each instance of these subclasses represents an instruction to measure and
  return the respective result for the given parameter values on a
  sequence of wires (subsystems).

Differentiation
^^^^^^^^^^^^^^^

In general, an :class:`Operation` is differentiable (at least using the finite-difference
method) with respect to a parameter iff

* the domain of that parameter is continuous.

For an :class:`Operation` to be differentiable with respect to a parameter using the
analytic method of differentiation, it must satisfy an additional constraint:

* the parameter domain must be real.

.. note::

    These conditions are *not* sufficient for analytic differentiation. For example,
    CV gates must also define a matrix representing their Heisenberg linear
    transformation on the quadrature operators.

CV Operation base classes
~~~~~~~~~~~~~~~~~~~~~~~~~

Due to additional requirements, continuous-variable (CV) operations must subclass the
:class:`~.CVOperation` or :class:`~.CVObservable` classes instead of :class:`~.Operation`
and :class:`~.Observable`.

Differentiation
^^^^^^^^^^^^^^^

To enable gradient computation using the analytic method for Gaussian CV operations, in addition, you need to
provide the static class method :meth:`~.CV._heisenberg_rep` that returns the Heisenberg representation of
the operation given its list of parameters, namely:

* For Gaussian CV Operations this method should return the matrix of the linear transformation carried out by the
  operation on the vector of quadrature operators :math:`\mathbf{r}` for the given parameter
  values.

* For Gaussian CV Observables this method should return a real vector (first-order observables)
  or symmetric matrix (second-order observables) of coefficients of the quadrature
  operators :math:`\x` and :math:`\p`.

PennyLane uses the convention :math:`\mathbf{r} = (\I, \x, \p)` for single-mode operations and observables
and :math:`\mathbf{r} = (\I, \x_0, \p_0, \x_1, \p_1, \ldots)` for multi-mode operations and observables.

.. note::
    Non-Gaussian CV operations and observables are currently only supported via
    the finite-difference method of gradient computation.
"""
# pylint:disable=access-member-before-definition
import abc
import copy
import itertools
import functools
import warnings
from enum import Enum, IntEnum
from scipy.sparse import kron, eye, coo_matrix

import numpy as np
from numpy.linalg import multi_dot

import pennylane as qml
from pennylane.wires import Wires

from .utils import pauli_eigs


def expand_matrix(base_matrix, wires, wire_order):
    """Re-express a base matrix acting on a subspace defined by a set of wire labels
    according to a global wire order.

    .. note::

        This function has essentially the same behaviour as :func:`.utils.expand` but is fully
        differentiable.

    Args:
        base_matrix (tensor_like): base matrix to expand
        wires (Iterable): wires determining the subspace that base matrix acts on; a base matrix of
            dimension :math:`2^n` acts on a subspace of :math:`n` wires
        wire_order (Iterable): global wire order, which has to contain all wire labels in ``wires``, but can also
            contain additional labels

    Returns:
        tensor_like: expanded matrix

    **Example**

    If the wire order is identical to ``wires``, the original matrix gets returned:

    >>> base_matrix = np.array([[1, 2, 3, 4],
    ...                         [5, 6, 7, 8],
    ...                         [9, 10, 11, 12],
    ...                         [13, 14, 15, 16]])
    >>> expand_matrix(base_matrix, wires=[0, 2], wire_order=[0, 2])
    [[ 1  2  3  4]
     [ 5  6  7  8]
     [ 9 10 11 12]
     [13 14 15 16]]

    If the wire order is a permutation of ``wires``, the entries of the base matrix get permuted:

    >>> expand_matrix(base_matrix, wires=[0, 2], wire_order=[2, 0])
    [[ 1  3  2  4]
     [ 9 11 10 12]
     [ 5  7  6  8]
     [13 15 14 16]]

    If the wire order contains wire labels not found in ``wires``, the matrix gets expanded:

    >>> expand_matrix(base_matrix, wires=[0, 2], wire_order=[0, 1, 2])
    [[ 1  2  0  0  3  4  0  0]
     [ 5  6  0  0  7  8  0  0]
     [ 0  0  1  2  0  0  3  4]
     [ 0  0  5  6  0  0  7  8]
     [ 9 10  0  0 11 12  0  0]
     [13 14  0  0 15 16  0  0]
     [ 0  0  9 10  0  0 11 12]
     [ 0  0 13 14  0  0 15 16]]

    The method works with tensors from all autodifferentiation frameworks, for example:

    >>> base_matrix_torch = torch.tensor([[1., 2.],
    ...                                   [3., 4.]], requires_grad=True)
    >>> res = expand_matrix(base_matrix_torch, wires=["b"], wire_order=["a", "b"])
    >>> type(res)
    <class 'torch.Tensor'>
    >>> res.requires_grad
    True
    """
    # TODO[Maria]: In future we should consider making ``utils.expand`` differentiable and calling it here.
    wire_order = Wires(wire_order)
    n = len(wires)
    interface = qml.math._multi_dispatch(base_matrix)  # pylint: disable=protected-access

    # operator's wire positions relative to wire ordering
    op_wire_pos = wire_order.indices(wires)

    I = qml.math.reshape(
        qml.math.eye(2 ** len(wire_order), like=interface), [2] * len(wire_order) * 2
    )
    axes = (list(range(n, 2 * n)), op_wire_pos)

    # reshape op.matrix()
    op_matrix_interface = qml.math.convert_like(base_matrix, I)
    mat_op_reshaped = qml.math.reshape(op_matrix_interface, [2] * n * 2)
    mat_tensordot = qml.math.tensordot(
        mat_op_reshaped, qml.math.cast_like(I, mat_op_reshaped), axes
    )

    unused_idxs = [idx for idx in range(len(wire_order)) if idx not in op_wire_pos]
    # permute matrix axes to match wire ordering
    perm = op_wire_pos + unused_idxs
    mat = qml.math.moveaxis(mat_tensordot, wire_order.indices(wire_order), perm)

    mat = qml.math.reshape(mat, (2 ** len(wire_order), 2 ** len(wire_order)))

    return mat


# =============================================================================
# Errors
# =============================================================================


class OperatorPropertyUndefined(Exception):
    """Generic exception to be used for undefined
    Operator properties or methods."""


class DecompositionUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's representation as a decomposition is undefined."""


class TermsUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's representation as a linear combination is undefined."""


class MatrixUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's matrix representation is undefined."""


class SparseMatrixUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's sparse matrix representation is undefined."""


class EigvalsUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's eigenvalues are undefined."""


class DiagGatesUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's diagonalizing gates are undefined."""


class AdjointUndefinedError(OperatorPropertyUndefined):
    """Raised when an Operator's adjoint version is undefined."""


class GeneratorUndefinedError(OperatorPropertyUndefined):
    """Exception used to indicate that an operator
    does not have a generator"""


# =============================================================================
# Wire types
# =============================================================================


class WiresEnum(IntEnum):
    """Integer enumeration class
    to represent the number of wires
    an operation acts on"""

    AnyWires = -1
    AllWires = 0


AllWires = WiresEnum.AllWires
"""IntEnum: An enumeration which represents all wires in the
subsystem. It is equivalent to an integer with value 0."""

AnyWires = WiresEnum.AnyWires
"""IntEnum: An enumeration which represents any wires in the
subsystem. It is equivalent to an integer with value -1."""


# =============================================================================
# ObservableReturnTypes types
# =============================================================================


class ObservableReturnTypes(Enum):
    """Enumeration class to represent the return types of an observable."""

    Sample = "sample"
    Variance = "var"
    Expectation = "expval"
    Probability = "probs"
    State = "state"

    def __repr__(self):
        """String representation of the return types."""
        return str(self.value)


Sample = ObservableReturnTypes.Sample
"""Enum: An enumeration which represents sampling an observable."""

Variance = ObservableReturnTypes.Variance
"""Enum: An enumeration which represents returning the variance of
an observable on specified wires."""

Expectation = ObservableReturnTypes.Expectation
"""Enum: An enumeration which represents returning the expectation
value of an observable on specified wires."""

Probability = ObservableReturnTypes.Probability
"""Enum: An enumeration which represents returning probabilities
of all computational basis states."""

State = ObservableReturnTypes.State
"""Enum: An enumeration which represents returning the state in the computational basis."""

# =============================================================================
# Class property
# =============================================================================


class ClassPropertyDescriptor:  # pragma: no cover
    """Allows a class property to be defined"""

    # pylint: disable=too-few-public-methods,too-many-public-methods
    def __init__(self, fget, fset=None):
        self.fget = fget
        self.fset = fset

    def __get__(self, obj, klass=None):
        if klass is None:
            klass = type(obj)
        return self.fget.__get__(obj, klass)()

    def __set__(self, obj, value):
        if not self.fset:
            raise AttributeError("can't set attribute")
        type_ = type(obj)
        return self.fset.__get__(obj, type_)(value)

    def setter(self, func):
        """Set the function as a class method, and store as an attribute."""
        if not isinstance(func, (classmethod, staticmethod)):
            func = classmethod(func)
        self.fset = func
        return self


def classproperty(func):
    """The class property decorator"""
    if not isinstance(func, (classmethod, staticmethod)):
        func = classmethod(func)

    return ClassPropertyDescriptor(func)


# =============================================================================
# Base Operator class
# =============================================================================


def _process_data(op):

    # Use qml.math.real to take the real part. We may get complex inputs for
    # example when differentiating holomorphic functions with JAX: a complex
    # valued QNode (one that returns qml.state) requires complex typed inputs.
    if op.name in ("RX", "RY", "RZ", "PhaseShift", "Rot"):
        return str([qml.math.round(qml.math.real(d) % (2 * np.pi), 10) for d in op.data])

    if op.name in ("CRX", "CRY", "CRZ", "CRot"):
        return str([qml.math.round(qml.math.real(d) % (4 * np.pi), 10) for d in op.data])

    return str(op.data)


class Operator(abc.ABC):
    r"""Base class for quantum operators supported by a device.

    ...
    * Representation by a **generator** via :math:`e^{G}` (:meth:`.Operator.generator`).

    Each representation method comes with a static method prefixed by ``compute_``, which
    takes the signature ``(*parameters, **hyperparameters)`` (for numerical representations that do not need
    to know about wire labels) or ``(*parameters, wires, **hyperparameters)``, where ``parameters``, ``wires``, and
    ``hyperparameters`` are the respective attributes of the operator class.

    Args:
        params (tuple[tensor_like]): trainable parameters
        wires (Iterable[Any] or Any): Wire label(s) that the operator acts on.
            If not given, args[-1] is interpreted as wires.
        do_queue (bool): indicates whether the operator should be
            recorded when created in a tape context
        id (str): custom label given to an operator instance,
            can be useful for some applications where the instance has to be identified

    **Example**

    A custom operator can be created by inheriting from :class:`~.Operator` or one of its subclasses.

    The following is an example for a custom gate that inherits from the :class:`~.Operation` subclass.
    It acts by potentially flipping a qubit and rotating another qubit.
    The custom operator defines a decomposition, which the devices can use (since it is unlikely that a device
    knows a native implementation for ``FlipAndRotate``). It also defines an adjoint operator.

    .. code-block:: python

        import pennylane as qml


        class FlipAndRotate(qml.operation.Operation):

            # Define how many wires the operator acts on in total.
            # In our case this may be one or two, which is why we
            # use the AnyWires Enumeration to indicate a variable number.
            num_wires = qml.operation.AnyWires

            # This attribute tells PennyLane what differentiation method to use. Here
            # we request parameter-shift (or "analytic") differentiation.
            grad_method = "A"

            def __init__(self, angle, wire_rot, wire_flip=None, do_flip=False,
                               do_queue=True, id=None):

                # checking the inputs --------------

                if do_flip and wire_flip is None:
                    raise ValueError("Expected a wire to flip; got None.")

                # note: we use the framework-agnostic math library since
                # trainable inputs could be tensors of different types
                shape = qml.math.shape(angle)
                if len(shape) > 1:
                    raise ValueError(f"Expected a scalar angle; got angle of shape {shape}.")

                #------------------------------------

                # do_flip is not trainable but influences the action of the operator,
                # which is why we define it to be a hyperparameter
                self._hyperparameters = {
                    "do_flip": do_flip
                }

                # we extract all wires that the operator acts on,
                # relying on the Wire class arithmetic
                all_wires = qml.wires.Wires(wire_rot) + qml.wires.Wires(wire_flip)

                # The parent class expects all trainable parameters to be fed as positional
                # arguments, and all wires acted on fed as a keyword argument.
                # The id keyword argument allows users to give their instance a custom name.
                # The do_queue keyword argument specifies whether or not
                # the operator is queued when created in a tape context.
                super().__init__(angle, wires=all_wires, do_queue=do_queue, id=id)

            @property
            def num_params(self):
                # if it is known before creation, define the number of parameters to expect here,
                # which makes sure an error is raised if the wrong number was passed
                return 1

            @staticmethod
            def compute_decomposition(angle, wires, do_flip):  # pylint: disable=arguments-differ
                # Overwriting this method defines the decomposition of the new gate, as it is
                # called by Operator.decomposition().
                # The general signature of this function is (*parameters, wires, **hyperparameters).
                op_list = []
                if do_flip:
                    op_list.append(qml.PauliX(wires=wires[1]))
                op_list.append(qml.RX(angle, wires=wires[0]))
                return op_list

            def adjoint(self):
                # the adjoint operator of this gate simply negates the angle
                return FlipAndRotate(-self.parameters[0], self.wires[0], self.wires[1], do_flip=self.hyperparameters["do_flip"])

    We can use the operation as follows:

    .. code-block:: python

        from pennylane import numpy as np

        dev = qml.device("default.qubit", wires=["q1", "q2", "q3"])

        @qml.qnode(dev)
        def circuit(angle):
            FlipAndRotate(angle, wire_rot="q1", wire_flip="q1")
            return qml.expval(qml.PauliZ("q1"))

    >>> a = np.array(3.14)
    >>> circuit(a)
    -0.9999987318946099

    """

    def __copy__(self):
        cls = self.__class__
        copied_op = cls.__new__(cls)
        copied_op.data = self.data.copy()
        for attr, value in vars(self).items():
            if attr != "data":
                setattr(copied_op, attr, value)

        return copied_op

    def __deepcopy__(self, memo):
        cls = self.__class__
        copied_op = cls.__new__(cls)

        # The memo dict maps object ID to object, and is required by
        # the deepcopy function to keep track of objects it has already
        # deep copied.
        memo[id(self)] = copied_op

        for attribute, value in self.__dict__.items():
            if attribute == "data":
                # Shallow copy the list of parameters. We avoid a deep copy
                # here, since PyTorch does not support deep copying of tensors
                # within a differentiable computation.
                copied_op.data = value.copy()
            else:
                # Deep copy everything else.
                setattr(copied_op, attribute, copy.deepcopy(value, memo))
        return copied_op

    @property
    def hash(self):
        """int: returns an integer hash uniquely representing the operator"""
        return hash((str(self.name), tuple(self.wires.tolist()), _process_data(self)))

    @staticmethod
    def compute_matrix(*params, **hyperparams):  # pylint:disable=unused-argument
        """Canonical matrix of this operator in the computational basis.

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. note::
            This method gets overwritten by subclasses to define the matrix representation
            of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in ``op.parameters``
            hyperparams (dict): non-trainable hyperparameters of this operator, as stored in ``op.hyperparameters``

        Returns:
            tensor_like: matrix representation

        **Example**

        >>> qml.CNOT.compute_matrix()
        [[1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 0, 1],
        [0, 0, 1, 0]]

        The matrix representation may depend on parameters or hyperparameters:

        >>> qml.Rot.compute_matrix(0.1, 0.2, 0.3)
        [[ 0.97517033-0.19767681j -0.09933467+0.00996671j]
         [ 0.09933467+0.00996671j  0.97517033+0.19767681j]]

        If parameters are tensors, a tensor of the same type is returned:

        >>> res = qml.Rot.compute_matrix(torch.tensor(0.1), torch.tensor(0.2), torch.tensor(0.3))
        >>> type(res)
        <class 'torch.Tensor'>
        """
        raise MatrixUndefinedError

    def matrix(self, wire_order=None):
        r"""Matrix representation of this operator in the computational basis.

        If ``wire_order`` is provided, the
        numerical representation considers the position of the
        operator's wires in the global wire order.
        Otherwise, the wire order defaults to ``self.wires``.

        If the matrix depends on trainable parameters, the result
        will be cast in the same autodifferentiation framework as the parameters.

        .. note::
            By default, this method calls the static method ``compute_matrix``,
            which is used by subclasses to define the actual matrix representation.

        A ``NotImplementedError`` is raised if the matrix representation has not been defined.

        Args:
            wire_order (Iterable): global wire order, must contain all wire labels from this operator's wires

        Returns:
            tensor_like: matrix representation

        **Example**

        >>> U = qml.PauliX(wires="b")
        >>> U.matrix()
        [[0 1]
         [1 0]]
        >>> U.matrix(wire_order=["a", "b"])
        [[0 1 0 0]
         [1 0 0 0]
         [0 0 0 1]
         [0 0 1 0]]
        >>> qml.RY(tf.Variable(0.5), wires="b").matrix()
        tf.Tensor([[ 0.9689124  -0.24740396]
                   [ 0.24740396  0.9689124 ]], shape=(2, 2), dtype=float32)

        """
        canonical_matrix = self.compute_matrix(*self.parameters, **self.hyperparameters)

        if wire_order is None or self.wires == Wires(wire_order):
            return canonical_matrix

        return expand_matrix(canonical_matrix, wires=self.wires, wire_order=wire_order)

    @staticmethod
    def compute_sparse_matrix(*params, **hyperparams):  # pylint:disable=unused-argument
        """Canonical matrix of this operator in the computational basis, using a sparse
        matrix type.

        The canonical matrix is the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        .. note::
            This method gets overwritten by subclasses to define the sparse matrix representation
            of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in ``op.parameters``
            hyperparams (dict): non-trainable hyperparameters of this operator, as stored in ``op.hyperparameters``

        Returns:
            scipy.sparse.coo.coo_matrix: matrix representation

        **Example**

        >>> from scipy.sparse import coo_matrix
        >>> H = np.array([[6+0j, 1-2j],[1+2j, -1]])
        >>> H = coo_matrix(H)
        >>> res = qml.SparseHamiltonian.compute_sparse_matrix(H)
        >>> res
        (0, 0)	(6+0j)
        (0, 1)	(1-2j)
        (1, 0)	(1+2j)
        (1, 1)	(-1+0j)
        >>> type(res)
        <class 'scipy.sparse.coo_matrix'>
        """
        raise SparseMatrixUndefinedError

    def sparse_matrix(self, wire_order=None):
        r"""Matrix representation of this operator in the computational basis, using
        a sparse matrix type.

        If ``wire_order`` is provided, the
        numerical representation considers the position of the
        operator's wires in the global wire order.
        Otherwise, the wire order defaults to ``self.wires``.

        .. note::
            By default, this method calls the static method ``compute_sparse_matrix``,
            which is used by subclasses to define the actual numerical representation.

        A ``NotImplementedError`` is raised if the matrix representation has not been defined.

        .. note::
            The wire_order argument is not yet implemented.

        Args:
            wire_order (Iterable): global wire order, must contain all wire labels from this operator's wires

        Returns:
            scipy.sparse.coo.coo_matrix: matrix representation

        **Example**

        >>> from scipy.sparse import coo_matrix
        >>> H = np.array([[6+0j, 1-2j],[1+2j, -1]])
        >>> H = coo_matrix(H)
        >>> res = qml.SparseHamiltonian(H, wires=[0]).sparse_matrix()
        >>> res
        (0, 0)	(6+0j)
        (0, 1)	(1-2j)
        (1, 0)	(1+2j)
        (1, 1)	(-1+0j)
        >>> type(res)
        <class 'scipy.sparse.coo_matrix'>
        """
        if wire_order is not None:
            raise NotImplementedError("The wire_order argument is not yet implemented")
        canonical_sparse_matrix = self.compute_sparse_matrix(
            *self.parameters, **self.hyperparameters
        )
        return canonical_sparse_matrix

    @staticmethod
    def compute_eigvals(*params, **hyperparams):
        """Eigenvalues of the operator in the computational basis.

        The eigenvalues refer to the textbook matrix representation that does not consider wires.
        Implicitly, this assumes that the wires of the operator correspond to the global wire order.

        This static method allows eigenvalues to be computed
        directly without instantiating the operator first.
        To return the eigenvalues of *instantiated* operators,
        please use the :meth:`~.Operator.eigvals()` method instead.

        If :attr:`diagonalizing_gates` are specified, the order of the
        eigenvalues matches the order of
        the computational basis vectors when the observable is
        diagonalized using these ops. Otherwise, no particular order is
        guaranteed.

        .. note::
            This method gets overwritten by subclasses to define the eigenvalues
            of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in ``op.parameters``
            hyperparams (dict): non-trainable hyperparameters of this operator, as stored in ``op.hyperparameters``

        Returns:
            array: eigenvalues

        **Example:**

        >>> qml.RZ.compute_eigvals(0.5)
        array([0.96891242-0.24740396j, 0.96891242+0.24740396j])
        >>> qml.PauliX(wires=0).diagonalizing_gates()
        [Hadamard(wires=[0])]
        >>> qml.PauliX.compute_eigvals()
        array([1, -1])
        """
        raise EigvalsUndefinedError

    def eigvals(self):
        r"""Eigenvalues of the operator.

        If :attr:`diagonalizing_gates` are specified, the order of the
        eigenvalues needs to match the order of
        the computational basis vectors when the observable is
        diagonalized using these ops. Otherwise, no particular order is
        guaranteed.

        .. note::
            By default, this method calls the static method ``compute_eigvals``,
            which is used by subclasses to define the actual eigenvalues. If no
            eigenvalues are defined, it is attempted to compute them from the matrix
            representation.

        Returns:
            array: eigenvalues

        **Example:**

        >>> U = qml.RZ(0.5, wires=1)
        >>> U.eigvals()
        array([0.96891242-0.24740396j, 0.96891242+0.24740396j])
        >>> qml.PauliX(wires=0).diagonalizing_gates()
        [Hadamard(wires=[0])]
        >>> qml.PauliX.eigvals()
        array([1, -1])
        """

        try:
            return self.compute_eigvals(*self.parameters, **self.hyperparameters)
        except EigvalsUndefinedError:
            # By default, compute the eigenvalues from the matrix representation.
            # This will raise a NotImplementedError if the matrix is undefined.
            try:
                return np.linalg.eigvals(self.matrix())
            except MatrixUndefinedError as e:
                raise EigvalsUndefinedError from e

    @staticmethod
    def compute_terms(*params, **hyperparams):  # pylint: disable=unused-argument
        r"""Static method to define the representation of this operation as a linear combination.

        Each term in the linear combination is a pair of a scalar
        value :math:`c_i` and an operator :math:`O_i`, so that the sum

        .. math:: O = \sum_i c_i O_i

        constructs this operator :math:`O`.

        A ``NotImplementedError`` is raised if no representation by terms is defined.

        .. note::
            This method gets overwritten by subclasses to define the linear combination representation
            of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in the ``parameters`` attribute
            hyperparams (dict): non-trainable hyperparameters of this operator, as stored in the
                ``hyperparameters`` attribute

        Returns:
            tuple[list[tensor_like or float], list[.Operation]]: list of coefficients and list of operations

        **Example**

        >>> qml.Hamiltonian().compute_terms([1., 2.], [qml.PauliX(0), qml.PauliZ(0)])
        [1., 2.], [qml.PauliX(0), qml.PauliZ(0)]
        """
        raise TermsUndefinedError

    def terms(self):
        r"""Representation of this operator as a linear combination.

        Each term in the linear combination is a pair of a
        scalar value :math:`c_i` and an operator :math:`O_i`, so that the sum

        .. math:: O = \sum_i c_i O_i

        constructs this operator :math:`O`.

        .. note::
            By default, this method calls the static method ``compute_terms``,
            which is used by subclasses to define the concrete representation.

        A ``NotImplementedError`` is raised if no representation through terms is defined.

        Returns:
            tuple[list[tensor_like or float], list[.Operation]]: list of coefficients :math:`c_i`
                and list of operations :math:`O_i`

        **Example**

        >>> qml.Hamiltonian([1., 2.], [qml.PauliX(0), qml.PauliZ(0)]).terms()
        [1., 2.], [qml.PauliX(0), qml.PauliZ(0)]

        The coefficients are differentiable and can be stored as tensors:

        >>> import tensorflow as tf
        >>> op = qml.Hamiltonian(tf.Variable([1., 2.]), [qml.PauliX(0), qml.PauliZ(0)])
        >>> op.terms()[0]
        [<tf.Tensor: shape=(), dtype=float32, numpy=1.0>, <tf.Tensor: shape=(), dtype=float32, numpy=2.0>]
        """
        return self.compute_terms(*self.parameters, **self.hyperparameters)

    @property
    @abc.abstractmethod
    def num_wires(self):
        """Number of wires the operator acts on."""

    @property
    def name(self):
        """String for the name of the operator."""
        return self._name

    @property
    def id(self):
        """String for the ID of the operator."""
        return self._id

    @name.setter
    def name(self, value):
        self._name = value

    def label(self, decimals=None, base_label=None):
        r"""A customizable string representation of the operator.

        Args:
            decimals=None (int): If ``None``, no parameters are included. Else,
                specifies how to round the parameters.
            base_label=None (str): overwrite the non-parameter component of the label

        Returns:
            str: label to use in drawings

        **Example:**

        >>> op = qml.RX(1.23456, wires=0)
        >>> op.label()
        "RX"
        >>> op.label(decimals=2)
        "RX\n(1.23)"
        >>> op.label(base_label="my_label")
        "my_label"
        >>> op.label(decimals=2, base_label="my_label")
        "my_label\n(1.23)"
        >>> op.inv()
        >>> op.label()
        "RX⁻¹"

        """
        op_label = base_label or self.__class__.__name__

        if decimals is None or self.num_params == 0:
            return op_label

        params = self.parameters

        # matrix parameters not rendered
        if len(qml.math.shape(params[0])) != 0:
            return op_label

        def _format(x):
            try:
                return format(qml.math.toarray(x), f".{decimals}f")
            except ValueError:
                # If the parameter can't be displayed as a float
                return format(x)

        if self.num_params == 1:
            return op_label + f"\n({_format(params[0])})"

        param_string = ",\n".join(_format(p) for p in params)
        return op_label + f"\n({param_string})"

    def __init__(self, *params, wires=None, do_queue=True, id=None):
        # pylint: disable=too-many-branches
        self._name = self.__class__.__name__  #: str: name of the operator
        self._id = id
        self.queue_idx = None  #: int, None: index of the Operator in the circuit queue, or None if not in a queue

        if wires is None:
            raise ValueError(f"Must specify the wires that {self.name} acts on")

        self._num_params = len(params)
        # Check if the expected number of parameters coincides with the one received.
        # This is always true for the default `Operator.num_params` property, but
        # subclasses may overwrite it to define a fixed expected value.
        if len(params) != self.num_params:
            raise ValueError(
                f"{self.name}: wrong number of parameters. "
                f"{len(params)} parameters passed, {self.num_params} expected."
            )

        if isinstance(wires, Wires):
            self._wires = wires
        else:
            self._wires = Wires(wires)  #: Wires: wires on which the operator acts

        # check that the number of wires given corresponds to required number
        if (
            self.num_wires != AllWires
            and self.num_wires != AnyWires
            and len(self._wires) != self.num_wires
        ):
            raise ValueError(
                f"{self.name}: wrong number of wires. "
                f"{len(self._wires)} wires given, {self.num_wires} expected."
            )

        self.data = list(params)  #: list[Any]: parameters of the operator

        if do_queue:
            self.queue()

    def __repr__(self):
        """Constructor-call-like representation."""
        if self.parameters:
            params = ", ".join([repr(p) for p in self.parameters])
            return f"{self.name}({params}, wires={self.wires.tolist()})"
        return f"{self.name}(wires={self.wires.tolist()})"

    @property
    def num_params(self):
        """Number of trainable parameters that this operator expects to be fed via the
        dynamic `*params` argument.

        By default, this property returns as many parameters as were used for the
        operator creation. If the number of parameters for an operator subclass is fixed,
        this property can be overwritten to return the fixed value.

        Returns:
            int: number of parameters
        """
        return self._num_params

    @property
    def wires(self):
        """Wires of this operator.

        Returns:
            Wires: wires
        """
        return self._wires

    @property
    def parameters(self):
        """Current parameter values."""
        return self.data.copy()

    @property
    def hyperparameters(self):
        """dict: Dictionary of non-trainable variables that define this operation."""
        # pylint: disable=attribute-defined-outside-init
        if hasattr(self, "_hyperparameters"):
            return self._hyperparameters
        self._hyperparameters = {}
        return self._hyperparameters

    def decomposition(self):
        r"""The decomposition of the Operator into a product of more fundamental gates.

        .. math:: O = O_1 O_2 \dots O_n

        .. note::
            By default, this method calls the static method
            :meth:`~.operation.Operator.compute_decomposition`. Unless the
            :meth:`~.operation.Operator.compute_decomposition` has a custom signature,
            this method should not be overwritten.

        Returns:
            list[Operator]: The decomposition of the Operator into lower level operations

        **Example:**

        >>> qml.IsingXX(1.23, wires=(0,1)).decomposition()
        [CNOT(wires=[0, 1]), RX(1.23, wires=[0]), CNOT(wires=[0, 1])]
        """
        return self.compute_decomposition(
            *self.parameters, wires=self.wires, **self.hyperparameters
        )

    @staticmethod
    def compute_decomposition(*params, wires=None, **hyperparameters):
        r"""Determine the Operator's decomposition for specified parameters, wires,
        and hyperparameters. The decomposition defines an Operator as a product of
        more fundamental gates:

        .. math:: O = O_1 O_2 \dots O_n.

        ``compute_decomposition`` is a static method and can provide the decomposition of an
        operator without a specific instance.

        .. seealso:: :meth:`~.operation.Operator.decomposition`.

        .. note::
            This method gets overwritten by subclasses, and the ``decomposition`` and
            ``expand`` methods rely on its definition. By default, this method should always
            take the Operator's parameters, wires, and hyperparameters as inputs, even if the
            decomposition is independent of these values.

        Args:
            *params: Variable length argument list.  Should match the ``parameters`` attribute

        Keyword Args:
            wires (Iterable, Wires): Wires that the operator acts on.
            **hyperparameters: Variable length keyword arguments.  Should match the
                ``hyperparameters`` attribute.

        Returns:
            list[Operator]: decomposition of the Operator into lower level operations

        **Example:**

        >>> qml.IsingXX.compute_decomposition(1.23, (0,1))
        [CNOT(wires=[0, 1]), RX(1.23, wires=[0]), CNOT(wires=[0, 1])]

        """
        raise DecompositionUndefinedError

    @staticmethod
    def compute_diagonalizing_gates(
        *params, wires, **hyperparams
    ):  # pylint: disable=unused-argument
        r"""Defines a partial representation of this operator via
        its eigendecomposition.

        Given the eigendecomposition :math:`O = U \Sigma U^{\dagger}` where
        :math:`\Sigma` is a diagonal matrix containing the eigenvalues,
        the sequence of diagonalizing gates implements the unitary :math:`U`.
        In other words, the diagonalizing gates rotate the state into the eigenbasis
        of this operator.

        This is the static version of ``diagonalizing_gates``, which can be called
        without creating an instance of the class.

        .. note::

            This method gets overwritten by subclasses to define the representation of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in ``op.parameters``
            wires (Iterable): trainable parameters of this operator, as stored in ``op.wires``
            hyperparams (dict): non-trainable hyperparameters of this operator, as stored in ``op.hyperparameters``

        Returns:
            list[.Operator]: A list of operators.

        **Example**

        >>> qml.PauliX.compute_diagonalizing_gates(wires="q1")
        [Hadamard(wires=["q1"])]
        """
        raise DiagGatesUndefinedError

    def diagonalizing_gates(self):  # pylint:disable=no-self-use
        r"""Defines a partial representation of this operator via
        its eigendecomposition.

        Given the eigendecomposition :math:`O = U \Sigma U^{\dagger}` where
        :math:`\Sigma` is a diagonal matrix containing the eigenvalues,
        the sequence of diagonalizing gates implements the unitary :math:`U`.
        In other words, the diagonalizing gates rotate the state into the eigenbasis
        of this operator.

        Returns ``None`` if this operator does not define its diagonalizing gates.

        .. note::

            By default, this method calls the static method ``compute_diagonalizing_gates``,
            which is used by subclasses to define the actual representation.

        Returns:
            list[.Operator] or None: a list of operators

        **Example**

        >>> qml.PauliX(wires="q1").diagonalizing_gates()
        [Hadamard(wires=["q1"])]
        """
        return self.compute_diagonalizing_gates(
            *self.parameters, wires=self.wires, **self.hyperparameters
        )

    def generator(self):  # pylint: disable=no-self-use
        r"""list[.Operation] or None: Generator of an operation
        with a single trainable parameter.

        For example, for operator

        .. math::

            U(\phi) = e^{i\phi (0.5 Y + Z\otimes X)}

        >>> U.generator()
          (0.5) [Y0]
        + (1.0) [Z0 X1]

        The generator may also be provided in the form of a dense or sparse Hamiltonian
        (using :class:`.Hermitian` and :class:`.SparseHamiltonian` respectively).

        The default value to return is ``None``, indicating that the operation has
        no defined generator.
        """
        raise GeneratorUndefinedError(f"Operation {self.name} does not have a generator")


    # =================================
    # Attributes interfacing with tapes
    # =================================

    def queue(self, context=qml.QueuingContext):
        """Append the operator to the Operator queue."""
        context.append(self)
        return self  # so pre-constructed Observable instances can be queued and returned in a single statement

    # ===============
    # Other utilities
    # ===============

    def label(self, decimals=None, base_label=None):
        r"""A customizable string representation of the operator.

        Args:
            decimals=None (int): If ``None``, no parameters are included. Else,
                specifies how to round the parameters.
            base_label=None (str): overwrite the non-parameter component of the label

        Returns:
            str: label to use in drawings

        **Example:**

        >>> op = qml.RX(1.23456, wires=0)
        >>> op.label()
        "RX"
        >>> op.label(decimals=2)
        "RX\n(1.23)"
        >>> op.label(base_label="my_label")
        "my_label"
        >>> op.label(decimals=2, base_label="my_label")
        "my_label\n(1.23)"
        >>> op.inv()
        >>> op.label()
        "RX⁻¹"

        """
        op_label = base_label or self.__class__.__name__

        if decimals is None or self.num_params == 0:
            return op_label

        params = self.parameters

        # matrix parameters not rendered
        if len(qml.math.shape(params[0])) != 0:
            return op_label

        def _format(x):
            try:
                return format(qml.math.toarray(x), f".{decimals}f")
            except ValueError:
                # If the parameter can't be displayed as a float
                return format(x)

        if self.num_params == 1:
            return op_label + f"\n({_format(params[0])})"

        param_string = ",\n".join(_format(p) for p in params)
        return op_label + f"\n({param_string})"

    def __repr__(self):
        """Constructor-call-like representation."""
        if self.parameters:
            params = ", ".join([repr(p) for p in self.parameters])
            return f"{self.name}({params}, wires={self.wires.tolist()})"
        return f"{self.name}(wires={self.wires.tolist()})"

    def __copy__(self):
        cls = self.__class__
        copied_op = cls.__new__(cls)
        copied_op.data = self.data.copy()
        for attr, value in vars(self).items():
            if attr != "data":
                setattr(copied_op, attr, value)

        return copied_op

    def __deepcopy__(self, memo):
        cls = self.__class__
        copied_op = cls.__new__(cls)

        # The memo dict maps object ID to object, and is required by
        # the deepcopy function to keep track of objects it has already
        # deep copied.
        memo[id(self)] = copied_op

        for attribute, value in self.__dict__.items():
            if attribute == "data":
                # Shallow copy the list of parameters. We avoid a deep copy
                # here, since PyTorch does not support deep copying of tensors
                # within a differentiable computation.
                copied_op.data = value.copy()
            else:
                # Deep copy everything else.
                setattr(copied_op, attribute, copy.deepcopy(value, memo))
        return copied_op

    @property
    def hash(self):
        """int: returns an integer hash uniquely representing the operator"""

        # Use qml.math.real to take the real part. We may get complex inputs for
        # example when differentiating holomorphic functions with JAX: a complex
        # valued QNode (one that returns qml.state) requires complex typed inputs.
        if self.name in ("RX", "RY", "RZ", "PhaseShift", "Rot"):
            data_string = str([qml.math.round(qml.math.real(d) % (2 * np.pi), 10) for d in self.data])

        elif self.name in ("CRX", "CRY", "CRZ", "CRot"):
            data_string = str([qml.math.round(qml.math.real(d) % (4 * np.pi), 10) for d in self.data])

        else:
            data_string = str(self.data)


# =============================================================================
# Base Operation class
# =============================================================================


class Operation(Operator):
    r"""Base class for quantum operations supported by a device.

    As with :class:`~.Operator`, the following class attributes must be
    defined for all operations:

    * :attr:`~.Operator.num_wires`

    The following two class attributes are optional, but in most cases
    should be clearly defined to avoid unexpected behavior during
    differentiation.

    * :attr:`~.Operation.grad_method`
    * :attr:`~.Operation.grad_recipe`

    Finally, there are some additional optional class attributes
    that may be set, and used by certain quantum optimizers:

    * :attr:`~.Operation.generator`

    Args:
        params (tuple[float, int, array]): operation parameters

    Keyword Args:
        wires (Sequence[int]): Subsystems it acts on. If not given, args[-1]
            is interpreted as wires.
        do_queue (bool): Indicates whether the operation should be
            immediately pushed into a :class:`BaseQNode` circuit queue.
            This flag is useful if there is some reason to run an Operation
            outside of a BaseQNode context.
    """

    @property
    def grad_method(self):
        """Gradient computation method.

        * ``'A'``: analytic differentiation using the parameter-shift method.
        * ``'F'``: finite difference numerical differentiation.
        * ``None``: the operation may not be differentiated.

        Default is ``'F'``, or ``None`` if the Operation has zero parameters.
        """
        return None if self.num_params == 0 else "F"

    grad_recipe = None
    r"""tuple(Union(list[list[float]], None)) or None: Gradient recipe for the
        parameter-shift method.

        This is a tuple with one nested list per operation parameter. For
        parameter :math:`\phi_k`, the nested list contains elements of the form
        :math:`[c_i, a_i, s_i]` where :math:`i` is the index of the
        term, resulting in a gradient recipe of

        .. math:: \frac{\partial}{\partial\phi_k}f = \sum_{i} c_i f(a_i \phi_k + s_i).

        If ``None``, the default gradient recipe containing the two terms
        :math:`[c_0, a_0, s_0]=[1/2, 1, \pi/2]` and :math:`[c_1, a_1,
        s_1]=[-1/2, 1, -\pi/2]` is assumed for every parameter.
    """

    basis = None
    """str or None: The basis of an operation, or for controlled gates, of the
    target operation. If not ``None``, should take a value of ``"X"``, ``"Y"``,
    or ``"Z"``.

    For example, ``X`` and ``CNOT`` have ``basis = "X"``, whereas
    ``ControlledPhaseShift`` and ``RZ`` have ``basis = "Z"``.
    """

    @property
    def control_wires(self):  # pragma: no cover
        r"""Returns the control wires.  For operations that are not controlled,
        this is an empty ``Wires`` object of length ``0``.

        Returns:
            Wires: The control wires of the operation.
        """
        return Wires([])

    @property
    def single_qubit_rot_angles(self):
        r"""The parameters required to implement a single-qubit gate as an
        equivalent ``Rot`` gate, up to a global phase.

        Returns:
            tuple[float, float, float]: A list of values :math:`[\phi, \theta, \omega]`
            such that :math:`RZ(\omega) RY(\theta) RZ(\phi)` is equivalent to the
            original operation.
        """
        raise NotImplementedError

    def get_parameter_shift(self, idx, shift=np.pi / 2):
        """Multiplier and shift for the given parameter, based on its gradient recipe.

        Args:
            idx (int): parameter index

        Returns:
            list[[float, float, float]]: list of multiplier, coefficient, shift for each term in the gradient recipe
        """
        # get the gradient recipe for this parameter
        recipe = self.grad_recipe[idx]

        # Default values
        multiplier = 0.5 / np.sin(shift)
        a = 1

        # We set the default recipe following:
        # ∂f(x) = c*f(a*x+s) - c*f(a*x-s)
        # where we express a positive and a negative shift by default
        default_param_shift = [[multiplier, a, shift], [-multiplier, a, -shift]]
        param_shift = default_param_shift if recipe is None else recipe
        return param_shift

    @property
    def inverse(self):
        """Boolean determining if the inverse of the operation was requested."""
        return self._inverse

    def adjoint(self, do_queue=False):  # pylint:disable=no-self-use
        """Create an operation that is the adjoint of this one.

        Adjointed operations are the conjugated and transposed version of the
        original operation. Adjointed ops are equivalent to the inverted operation for unitary
        gates.

        Args:
            do_queue: Whether to add the adjointed gate to the context queue.

        Returns:
            The adjointed operation.
        """
        raise AdjointUndefinedError

    @inverse.setter
    def inverse(self, boolean):
        self._inverse = boolean

    def expand(self):
        """Returns a tape containing the decomposed operations, rather
        than a list.

        Returns:
            .JacobianTape: Returns a quantum tape that contains the
            operations decomposition, or if not implemented, simply
            the operation itself.
        """
        tape = qml.tape.QuantumTape(do_queue=False)

        with tape:
            self.decomposition()

        if not self.data:
            # original operation has no trainable parameters
            tape.trainable_params = {}

        if self.inverse:
            tape.inv()

        return tape

    def inv(self):
        """Inverts the operation, such that the inverse will
        be used for the computations by the specific device.

        This method concatenates a string to the name of the operation,
        to indicate that the inverse will be used for computations.

        Any subsequent call of this method will toggle between the original
        operation and the inverse of the operation.

        Returns:
            :class:`Operator`: operation to be inverted
        """
        if qml.QueuingContext.recording():
            current_inv = qml.QueuingContext.get_info(self).get("inverse", False)
            qml.QueuingContext.update_info(self, inverse=not current_inv)
        else:
            self.inverse = not self._inverse
        return self

    def matrix(self, wire_order=None):
        canonical_matrix = self.compute_matrix(*self.parameters, **self.hyperparameters)

        if self.inverse:
            canonical_matrix = qml.math.conj(qml.math.T(canonical_matrix))

        if wire_order is None or self.wires == Wires(wire_order):
            return canonical_matrix

        return expand_matrix(canonical_matrix, wires=self.wires, wire_order=wire_order)

    def eigvals(self):
        op_eigvals = super().eigvals()

        if self.inverse:
            return qml.math.conj(op_eigvals)

        return op_eigvals

    @property
    def base_name(self):
        """Get base name of the operator."""
        return self.__class__.__name__

    @property
    def name(self):
        """Get and set the name of the operator."""
        return self._name + ".inv" if self.inverse else self._name

    def label(self, decimals=None, base_label=None):
        if self.inverse:
            base_label = base_label or self.__class__.__name__
            base_label += "⁻¹"
        return super().label(decimals=decimals, base_label=base_label)

    def __init__(self, *params, wires=None, do_queue=True, id=None):

        self._inverse = False
        super().__init__(*params, wires=wires, do_queue=do_queue, id=id)

        # check the grad_recipe validity
        if self.grad_method == "A":
            if self.grad_recipe is None:
                # default recipe for every parameter
                self.grad_recipe = [None] * self.num_params
            else:
                assert (
                    len(self.grad_recipe) == self.num_params
                ), "Gradient recipe must have one entry for each parameter!"
        else:
            assert self.grad_recipe is None, "Gradient recipe is only used by the A method!"


class Channel(Operation, abc.ABC):
    r"""Base class for quantum channels.

    As with :class:`~.Operation`, the following class attributes must be
    defined for all channels:

    * :attr:`~.Operator.num_wires`

    To define a noisy channel, the following attribute of :class:`~.Channel`
    can be used to list the corresponding Kraus matrices.

    * :attr:`~.Channel._kraus_matrices`

    The following two class attributes are optional, but in most cases
    should be clearly defined to avoid unexpected behavior during
    differentiation.

    * :attr:`~.Operation.grad_method`
    * :attr:`~.Operation.grad_recipe`

    Args:
        params (tuple[float, int, array]): operation parameters

    Keyword Args:
        wires (Sequence[int]): Subsystems the channel acts on. If not given, args[-1]
            is interpreted as wires.
        do_queue (bool): Indicates whether the operation should be
            immediately pushed into a :class:`BaseQNode` circuit queue.
            This flag is useful if there is some reason to run an Operation
            outside of a BaseQNode context.
    """
    # pylint: disable=abstract-method

    @staticmethod
    @abc.abstractmethod
    def compute_kraus_matrices(*params, **hyperparams):  # pylint:disable=unused-argument
        """Kraus matrices representing a quantum channel, specified in
        the computational basis.

        This is a static method that should be defined for all
        new channels, and which allows matrices to be computed
        directly without instantiating the channel first.

        To return the Kraus matrices of an *instantiated* channel,
        please use the :meth:`~.Operator.kraus_matrices()` method instead.

        .. note::
            This method gets overwritten by subclasses to define the kraus matrix representation
            of a particular operator.

        Args:
            params (list): trainable parameters of this operator, as stored in the ``parameters`` attribute
            hyperparams (dict): non-trainable hyperparameters of this operator,
                as stored in the ``hyperparameters`` attribute

        Returns:
            list (array): list of Kraus matrices

        **Example**

        >>> qml.AmplitudeDamping.compute_kraus_matrices(0.1)
        [array([[1., 0.], [0., 0.9486833]]),
         array([[0., 0.31622777], [0., 0.]])]
        """
        raise NotImplementedError

    def kraus_matrices(self):
        r"""Kraus matrices of an instantiated channel
        in the computational basis.

        Returns:
            list (array): list of Kraus matrices

        ** Example**

        >>> U = qml.AmplitudeDamping(0.1, wires=1)
        >>> U.kraus_matrices()
        [array([[1., 0.], [0., 0.9486833]]),
         array([[0., 0.31622777], [0., 0.]])]
        """
        return self.compute_kraus_matrices(*self.parameters, **self.hyperparameters)


# =============================================================================
# Base Observable class
# =============================================================================


class Observable(Operator):
    """Base class for observables supported by a device.

    :class:`Observable` is used to describe Hermitian quantum observables.

    As with :class:`~.Operator`, the following class attributes must be
    defined for all observables:

    * :attr:`~.Operator.num_wires`

    Args:
        params (tuple[float, int, array]): observable parameters

    Keyword Args:
        wires (Sequence[int]): subsystems it acts on.
            Currently, only one subsystem is supported.
        do_queue (bool): Indicates whether the operation should be
            immediately pushed into the Operator queue.
    """

    # pylint: disable=abstract-method
    return_type = None

    def __init__(self, *params, wires=None, do_queue=True, id=None):
        # extract the arguments
        if wires is None:
            try:
                wires = params[-1]
                params = params[:-1]
                # error if no arguments are given
            except IndexError as err:
                raise ValueError(
                    f"Must specify the wires that {type(self).__name__} acts on"
                ) from err

        super().__init__(*params, wires=wires, do_queue=do_queue, id=id)

    def __repr__(self):
        """Constructor-call-like representation."""
        temp = super().__repr__()

        if self.return_type is None:
            return temp

        if self.return_type is Probability:
            return repr(self.return_type) + f"(wires={self.wires.tolist()})"

        return repr(self.return_type) + "(" + temp + ")"

    def __matmul__(self, other):
        if isinstance(other, Tensor):
            return other.__rmatmul__(self)

        if isinstance(other, Observable):
            return Tensor(self, other)

        raise ValueError("Can only perform tensor products between observables.")

    def _obs_data(self):
        r"""Extracts the data from a Observable or Tensor and serializes it in an order-independent fashion.

        This allows for comparison between observables that are equivalent, but are expressed
        in different orders. For example, `qml.PauliX(0) @ qml.PauliZ(1)` and
        `qml.PauliZ(1) @ qml.PauliX(0)` are equivalent observables with different orderings.

        **Example**

        >>> tensor = qml.PauliX(0) @ qml.PauliZ(1)
        >>> print(tensor._obs_data())
        {("PauliZ", <Wires = [1]>, ()), ("PauliX", <Wires = [0]>, ())}
        """
        obs = Tensor(self).non_identity_obs
        tensor = set()

        for ob in obs:
            parameters = tuple(param.tobytes() for param in ob.parameters)
            tensor.add((ob.name, ob.wires, parameters))

        return tensor

    def compare(self, other):
        r"""Compares with another :class:`~.Hamiltonian`, :class:`~Tensor`, or :class:`~Observable`,
        to determine if they are equivalent.

        Observables/Hamiltonians are equivalent if they represent the same operator
        (their matrix representations are equal), and they are defined on the same wires.

        .. Warning::

            The compare method does **not** check if the matrix representation
            of a :class:`~.Hermitian` observable is equal to an equivalent
            observable expressed in terms of Pauli matrices.
            To do so would require the matrix form of Hamiltonians and Tensors
            be calculated, which would drastically increase runtime.

        Returns:
            (bool): True if equivalent.

        **Examples**

        >>> ob1 = qml.PauliX(0) @ qml.Identity(1)
        >>> ob2 = qml.Hamiltonian([1], [qml.PauliX(0)])
        >>> ob1.compare(ob2)
        True
        >>> ob1 = qml.PauliX(0)
        >>> ob2 = qml.Hermitian(np.array([[0, 1], [1, 0]]), 0)
        >>> ob1.compare(ob2)
        False
        """
        if isinstance(other, qml.Hamiltonian):
            return other.compare(self)
        if isinstance(other, (Tensor, Observable)):
            return other._obs_data() == self._obs_data()

        raise ValueError(
            "Can only compare an Observable/Tensor, and a Hamiltonian/Observable/Tensor."
        )

    def __add__(self, other):
        r"""The addition operation between Observables/Tensors/qml.Hamiltonian objects."""
        if isinstance(other, qml.Hamiltonian):
            return other + self
        if isinstance(other, (Observable, Tensor)):
            return qml.Hamiltonian([1, 1], [self, other], simplify=True)
        raise ValueError(f"Cannot add Observable and {type(other)}")

    def __mul__(self, a):
        r"""The scalar multiplication operation between a scalar and an Observable/Tensor."""
        if isinstance(a, (int, float)):

            return qml.Hamiltonian([a], [self], simplify=True)

        raise ValueError(f"Cannot multiply Observable by {type(a)}")

    __rmul__ = __mul__

    def __sub__(self, other):
        r"""The subtraction operation between Observables/Tensors/qml.Hamiltonian objects."""
        if isinstance(other, (Observable, Tensor, qml.Hamiltonian)):
            return self.__add__(other.__mul__(-1))
        raise ValueError(f"Cannot subtract {type(other)} from Observable")


class Tensor(Observable):
    """Container class representing tensor products of observables.

    To create a tensor, simply initiate it like so:

    >>> T = Tensor(qml.PauliX(0), qml.Hermitian(A, [1, 2]))

    You can also create a tensor from other Tensors:

    >>> T = Tensor(T, qml.PauliZ(4))

    The ``@`` symbol can be used as a tensor product operation:

    >>> T = qml.PauliX(0) @ qml.Hadamard(2)
    """

    # pylint: disable=abstract-method
    return_type = None
    tensor = True

    def __init__(self, *args):  # pylint: disable=super-init-not-called
        self._eigvals_cache = None
        self.obs = []
        self._args = args
        self.queue(init=True)

    def label(self, decimals=None, base_label=None):
        r"""How the operator is represented in diagrams and drawings.

        Args:
            decimals=None (Int): If ``None``, no parameters are included. Else,
                how to round the parameters.
            base_label=None (Iterable[str]): overwrite the non-parameter component of the label.
                Must be same length as ``obs`` attribute.

        Returns:
            str: label to use in drawings

        >>> T = qml.PauliX(0) @ qml.Hadamard(2)
        >>> T.label()
        'X@H'
        >>> T.label(base_label=["X0", "H2"])
        'X0@H2'

        """
        if base_label is not None:
            if len(base_label) != len(self.obs):
                raise ValueError(
                    "Tensor label requires ``base_label`` keyword to be same length"
                    " as tensor components."
                )
            return "@".join(
                ob.label(decimals=decimals, base_label=lbl) for ob, lbl in zip(self.obs, base_label)
            )

        return "@".join(ob.label(decimals=decimals) for ob in self.obs)

    def queue(self, context=qml.QueuingContext, init=False):  # pylint: disable=arguments-differ
        constituents = self.obs

        if init:
            constituents = self._args

        for o in constituents:

            if init:
                if isinstance(o, Tensor):
                    self.obs.extend(o.obs)
                elif isinstance(o, Observable):
                    self.obs.append(o)
                else:
                    raise ValueError("Can only perform tensor products between observables.")

            try:
                context.update_info(o, owner=self)
            except qml.queuing.QueuingError:
                o.queue(context=context)
                context.update_info(o, owner=self)
            except NotImplementedError:
                pass

        context.append(self, owns=tuple(constituents))
        return self

    def __copy__(self):
        cls = self.__class__
        copied_op = cls.__new__(cls)
        copied_op.obs = self.obs.copy()
        copied_op._eigvals_cache = self._eigvals_cache
        return copied_op

    def __repr__(self):
        """Constructor-call-like representation."""

        s = " @ ".join([repr(o) for o in self.obs])

        if self.return_type is None:
            return s

        if self.return_type is Probability:
            return repr(self.return_type) + f"(wires={self.wires.tolist()})"

        return repr(self.return_type) + "(" + s + ")"

    @property
    def name(self):
        """All constituent observable names making up the tensor product.

        Returns:
            list[str]: list containing all observable names
        """
        return [o.name for o in self.obs]

    @property
    def num_wires(self):
        """Number of wires the tensor product acts on.

        Returns:
            int: number of wires
        """
        return len(self.wires)

    @property
    def wires(self):
        """All wires in the system the tensor product acts on.

        Returns:
            Wires: wires addressed by the observables in the tensor product
        """
        return Wires.all_wires([o.wires for o in self.obs])

    @property
    def data(self):
        """Raw parameters of all constituent observables in the tensor product.

        Returns:
            list[Any]: flattened list containing all dependent parameters
        """
        return sum((o.data for o in self.obs), [])

    @property
    def num_params(self):
        """Raw parameters of all constituent observables in the tensor product.

        Returns:
            list[Any]: flattened list containing all dependent parameters
        """
        return len(self.data)

    @property
    def parameters(self):
        """Evaluated parameter values of all constituent observables in the tensor product.

        Returns:
            list[list[Any]]: nested list containing the parameters per observable
            in the tensor product
        """
        return [o.parameters for o in self.obs]

    @property
    def non_identity_obs(self):
        """Returns the non-identity observables contained in the tensor product.

        Returns:
            list[:class:`~.Observable`]: list containing the non-identity observables
            in the tensor product
        """
        return [obs for obs in self.obs if not isinstance(obs, qml.Identity)]

    def __matmul__(self, other):
        if isinstance(other, Tensor):
            self.obs.extend(other.obs)

        elif isinstance(other, Observable):
            self.obs.append(other)

        else:
            raise ValueError("Can only perform tensor products between observables.")

        if qml.QueuingContext.recording():
            owning_info = qml.QueuingContext.get_info(self)["owns"] + (other,)

            # update the annotated queue information
            qml.QueuingContext.update_info(self, owns=owning_info)
            qml.QueuingContext.update_info(other, owner=self)

        return self

    def __rmatmul__(self, other):
        if isinstance(other, Observable):
            self.obs[:0] = [other]
            if qml.QueuingContext.recording():
                qml.QueuingContext.update_info(other, owner=self)
            return self

        raise ValueError("Can only perform tensor products between observables.")

    __imatmul__ = __matmul__

    def eigvals(self):
        """Return the eigenvalues of the specified tensor product observable.

        This method uses pre-stored eigenvalues for standard observables where
        possible.

        Returns:
            array[float]: array containing the eigenvalues of the tensor product
            observable
        """
        if self._eigvals_cache is not None:
            return self._eigvals_cache

        standard_observables = {"PauliX", "PauliY", "PauliZ", "Hadamard"}

        # observable should be Z^{\otimes n}
        self._eigvals_cache = pauli_eigs(len(self.wires))

        # Sort observables lexicographically by the strings of the wire labels
        # TODO: check for edge cases of the sorting, e.g. Tensor(Hermitian(obs, wires=[0, 2]),
        # Hermitian(obs, wires=[1, 3, 4])
        # Sorting the observables based on wires, so that the order of
        # the eigenvalues is correct
        obs_sorted = sorted(self.obs, key=lambda x: [str(l) for l in x.wires.labels])

        # check if there are any non-standard observables (such as Identity)
        if set(self.name) - standard_observables:
            # Tensor product of observables contains a mixture
            # of standard and non-standard observables
            self._eigvals_cache = np.array([1])
            for k, g in itertools.groupby(obs_sorted, lambda x: x.name in standard_observables):
                if k:
                    # Subgroup g contains only standard observables.
                    self._eigvals_cache = np.kron(self._eigvals_cache, pauli_eigs(len(list(g))))
                else:
                    # Subgroup g contains only non-standard observables.
                    for ns_ob in g:
                        # loop through all non-standard observables
                        self._eigvals_cache = np.kron(self._eigvals_cache, ns_ob.eigvals())

        return self._eigvals_cache

    def diagonalizing_gates(self):
        """Return the gate set that diagonalizes a circuit according to the
        specified tensor observable.

        This method uses pre-stored eigenvalues for standard observables where
        possible and stores the corresponding eigenvectors from the eigendecomposition.

        Returns:
            list: list containing the gates diagonalizing the tensor observable
        """
        diag_gates = []
        for o in self.obs:
            diag_gates.extend(o.diagonalizing_gates())

        return diag_gates

    def matrix(self, wire_order=None):
        r"""Matrix representation of the Tensor operator
        in the computational basis.

        .. note::

            The wire_order argument is added for compatibility, but currently not implemented.
            The Tensor class is planned to be removed soon.

        Args:
            wire_order (Iterable): global wire order, must contain all wire labels in this operator's wires

        Returns:
            array: matrix representation

        **Example**

        >>> O = qml.PauliZ(0) @ qml.PauliZ(2)
        >>> O.matrix()
        array([[ 1,  0,  0,  0],
               [ 0, -1,  0,  0],
               [ 0,  0, -1,  0],
               [ 0,  0,  0,  1]])

        To get the full :math:`2^3\times 2^3` Hermitian matrix
        acting on the 3-qubit system, the identity on wire 1
        must be explicitly included:

        >>> O = qml.PauliZ(0) @ qml.Identity(1) @ qml.PauliZ(2)
        >>> O.matrix()
        array([[ 1.,  0.,  0.,  0.,  0.,  0.,  0.,  0.],
               [ 0., -1.,  0., -0.,  0., -0.,  0., -0.],
               [ 0.,  0.,  1.,  0.,  0.,  0.,  0.,  0.],
               [ 0., -0.,  0., -1.,  0., -0.,  0., -0.],
               [ 0.,  0.,  0.,  0., -1., -0., -0., -0.],
               [ 0., -0.,  0., -0., -0.,  1., -0.,  0.],
               [ 0.,  0.,  0.,  0., -0., -0., -1., -0.],
               [ 0., -0.,  0., -0., -0.,  0., -0.,  1.]])
        """

        if wire_order is not None:
            raise NotImplementedError("The wire_order argument is currently not implemented.")

        # Check for partially (but not fully) overlapping wires in the observables
        partial_overlap = self.check_wires_partial_overlap()

        # group the observables based on what wires they act on
        U_list = []
        for _, g in itertools.groupby(self.obs, lambda x: x.wires.labels):
            # extract the matrices of each diagonalizing gate
            mats = [i.matrix() for i in g]

            if len(mats) > 1:
                # multiply all unitaries together before appending
                mats = [multi_dot(mats)]

            # append diagonalizing unitary for specific wire to U_list
            U_list.append(mats[0])

        mat_size = np.prod([np.shape(mat)[0] for mat in U_list])
        wire_size = 2 ** len(self.wires)
        if mat_size != wire_size:
            if partial_overlap:
                warnings.warn(
                    "The matrix for Tensors of Tensors/Observables with partially "
                    "overlapping wires might yield unexpected results. In particular "
                    "the matrix size might be larger than intended."
                )
            else:
                warnings.warn(
                    f"The size of the returned matrix ({mat_size}) will not be compatible "
                    f"with the subspace of the wires of the Tensor ({wire_size}). "
                    "This likely is due to wires being used in multiple tensor product "
                    "factors of the Tensor."
                )

        # Return the Hermitian matrix representing the observable
        # over the defined wires.
        return functools.reduce(np.kron, U_list)

    def check_wires_partial_overlap(self):
        r"""Tests whether any two observables in the Tensor have partially
        overlapping wires and raise a warning if they do.

        .. note::

            Fully overlapping wires, i.e., observables with
            same (sets of) wires are not reported, as the ``matrix`` method is
            well-defined and implemented for this scenario.
        """
        for o1, o2 in itertools.combinations(self.obs, r=2):
            shared = qml.wires.Wires.shared_wires([o1.wires, o2.wires])
            if shared and (shared != o1.wires or shared != o2.wires):
                return 1
        return 0

    def sparse_matrix(self, wires=None):  # pylint:disable=arguments-renamed
        r"""Computes a `scipy.sparse.coo_matrix` representation of this Tensor.

        This is useful for larger qubit numbers, where the dense matrix becomes very large, while
        consisting mostly of zero entries.

        Args:
            wires (Iterable): Wire labels that indicate the order of wires according to which the matrix
                is constructed. If not provided, ``self.wires`` is used.

        Returns:
            :class:`scipy.sparse.coo_matrix`: sparse matrix representation

        **Example**

        Consider the following tensor:

        >>> t = qml.PauliX(0) @ qml.PauliZ(1)

        Without passing wires, the sparse representation is given by:

        >>> print(t.sparse_matrix())
        (0, 2)	1
        (1, 3)	-1
        (2, 0)	1
        (3, 1)	-1

        If we define a custom wire ordering, the matrix representation changes
        accordingly:
        >>> print(t.sparse_matrix(wires=[1, 0]))
        (0, 1)	1
        (1, 0)	1
        (2, 3)	-1
        (3, 2)	-1

        We can also enforce implicit identities by passing wire labels that
        are not present in the consituent operations:

        >>> res = t.sparse_matrix(wires=[0, 1, 2])
        >>> print(res.shape)
        (8, 8)
        """

        if wires is None:
            wires = self.wires
        else:
            wires = Wires(wires)

        list_of_sparse_ops = [eye(2, format="coo")] * len(wires)

        for o in self.obs:
            if len(o.wires) > 1:
                # todo: deal with multi-qubit operations that do not act on consecutive qubits
                raise ValueError(
                    f"Can only compute sparse representation for tensors whose operations "
                    f"act on consecutive wires; got {o}."
                )
            # store the single-qubit ops according to the order of their wires
            idx = wires.index(o.wires)
            list_of_sparse_ops[idx] = coo_matrix(o.matrix())

        return functools.reduce(lambda i, j: kron(i, j, format="coo"), list_of_sparse_ops)

    def prune(self):
        """Returns a pruned tensor product of observables by removing :class:`~.Identity` instances from
        the observables building up the :class:`~.Tensor`.

        The ``return_type`` attribute is preserved while pruning.

        If the tensor product only contains one observable, then this observable instance is
        returned.

        Note that, as a result, this method can return observables that are not a :class:`~.Tensor`
        instance.

        **Example:**

        Pruning that returns a :class:`~.Tensor`:

        >>> O = qml.PauliZ(0) @ qml.Identity(1) @ qml.PauliZ(2)
        >>> O.prune()
        <pennylane.operation.Tensor at 0x7fc1642d1590
        >>> [(o.name, o.wires) for o in O.prune().obs]
        [('PauliZ', [0]), ('PauliZ', [2])]

        Pruning that returns a single observable:

        >>> O = qml.PauliZ(0) @ qml.Identity(1)
        >>> O_pruned = O.prune()
        >>> (O_pruned.name, O_pruned.wires)
        ('PauliZ', [0])

        Returns:
            ~.Observable: the pruned tensor product of observables
        """
        if len(self.non_identity_obs) == 0:
            # Return a single Identity as the tensor only contains Identities
            obs = qml.Identity(self.wires[0])
        elif len(self.non_identity_obs) == 1:
            obs = self.non_identity_obs[0]
        else:
            obs = Tensor(*self.non_identity_obs)

        obs.return_type = self.return_type
        return obs


# =============================================================================
# CV Operations and observables
# =============================================================================


class CV:
    """A mixin base class denoting a continuous-variable operation."""

    # pylint: disable=no-member

    def heisenberg_expand(self, U, wire_order):
        """Expand the given local Heisenberg-picture array into a full-system one.

        Args:
            U (array[float]): array to expand (expected to be of the dimension ``1+2*self.num_wires``)
            wire_order (Wires): global wire order defining which subspace the operator acts on

        Raises:
            ValueError: if the size of the input matrix is invalid or `num_wires` is incorrect

        Returns:
            array[float]: expanded array, dimension ``1+2*num_wires``
        """

        U_dim = len(U)
        nw = len(self.wires)

        if U.ndim > 2:
            raise ValueError("Only order-1 and order-2 arrays supported.")

        if U_dim != 1 + 2 * nw:
            raise ValueError(f"{self.name}: Heisenberg matrix is the wrong size {U_dim}.")

        if len(wire_order) == 0 or len(self.wires) == len(wire_order):
            # no expansion necessary (U is a full-system matrix in the correct order)
            return U

        if not wire_order.contains_wires(self.wires):
            raise ValueError(
                f"{self.name}: Some observable wires {self.wires} do not exist on this device with wires {wire_order}"
            )

        # get the indices that the operation's wires have on the device
        wire_indices = wire_order.indices(self.wires)

        # expand U into the I, x_0, p_0, x_1, p_1, ... basis
        dim = 1 + len(wire_order) * 2

        def loc(w):
            "Returns the slice denoting the location of (x_w, p_w) in the basis."
            ind = 2 * w + 1
            return slice(ind, ind + 2)

        if U.ndim == 1:
            W = np.zeros(dim)
            W[0] = U[0]
            for k, w in enumerate(wire_indices):
                W[loc(w)] = U[loc(k)]
        elif U.ndim == 2:
            if isinstance(self, Observable):
                W = np.zeros((dim, dim))
            else:
                W = np.eye(dim)

            W[0, 0] = U[0, 0]

            for k1, w1 in enumerate(wire_indices):
                s1 = loc(k1)
                d1 = loc(w1)

                # first column
                W[d1, 0] = U[s1, 0]
                # first row (for gates, the first row is always (1, 0, 0, ...), but not for observables!)
                W[0, d1] = U[0, s1]

                for k2, w2 in enumerate(wire_indices):
                    W[d1, loc(w2)] = U[s1, loc(k2)]  # block k1, k2 in U goes to w1, w2 in W.
        return W

    @staticmethod
    def _heisenberg_rep(p):
        r"""Heisenberg picture representation of the operation.

        * For Gaussian CV gates, this method returns the matrix of the linear
          transformation carried out by the gate for the given parameter values.
          The method is not defined for non-Gaussian gates.

          **The existence of this method is equivalent to setting** ``grad_method = 'A'``.

        * For observables, returns a real vector (first-order observables) or
          symmetric matrix (second-order observables) of expansion coefficients
          of the observable.

        For single-mode Operations we use the basis :math:`\mathbf{r} = (\I, \x, \p)`.
        For multi-mode Operations we use the basis :math:`\mathbf{r} = (\I, \x_0, \p_0, \x_1, \p_1, \ldots)`.

        .. note::

            For gates, we assume that the inverse transformation is obtained
            by negating the first parameter.

        Args:
            p (Sequence[float]): parameter values for the transformation

        Returns:
            array[float]: :math:`\tilde{U}` or :math:`q`
        """
        # pylint: disable=unused-argument
        return None

    @classproperty
    def supports_heisenberg(self):
        """Returns True iff the CV Operation has overridden the :meth:`~.CV._heisenberg_rep`
        static method, thereby indicating that it is Gaussian and does not block the use
        of the parameter-shift differentiation method if found between the differentiated gate
        and an observable.
        """
        return CV._heisenberg_rep != self._heisenberg_rep


class CVOperation(CV, Operation):
    """Base class for continuous-variable quantum operations."""

    # pylint: disable=abstract-method

    @classproperty
    def supports_parameter_shift(self):
        """Returns True iff the CV Operation supports the parameter-shift differentiation method.
        This means that it has ``grad_method='A'`` and
        has overridden the :meth:`~.CV._heisenberg_rep` static method.
        """
        return self.grad_method == "A" and self.supports_heisenberg

    def heisenberg_pd(self, idx):
        """Partial derivative of the Heisenberg picture transform matrix.

        Computed using grad_recipe.

        Args:
            idx (int): index of the parameter with respect to which the
                partial derivative is computed.
        Returns:
            array[float]: partial derivative
        """
        # get the gradient recipe for this parameter
        recipe = self.grad_recipe[idx]

        # Default values
        multiplier = 0.5
        a = 1
        shift = np.pi / 2

        # We set the default recipe to as follows:
        # ∂f(x) = c*f(x+s) - c*f(x-s)
        default_param_shift = [[multiplier, a, shift], [-multiplier, a, -shift]]
        param_shift = default_param_shift if recipe is None else recipe

        pd = None  # partial derivative of the transformation

        p = self.parameters

        original_p_idx = p[idx]
        for c, _a, s in param_shift:
            # evaluate the transform at the shifted parameter values
            p[idx] = _a * original_p_idx + s
            U = self._heisenberg_rep(p)  # pylint: disable=assignment-from-none

            if pd is None:
                pd = c * U
            else:
                pd += c * U

        return pd

    def heisenberg_tr(self, wire_order, inverse=False):
        r"""Heisenberg picture representation of the linear transformation carried
        out by the gate at current parameter values.

        Given a unitary quantum gate :math:`U`, we may consider its linear
        transformation in the Heisenberg picture, :math:`U^\dagger(\cdot) U`.

        If the gate is Gaussian, this linear transformation preserves the polynomial order
        of any observables that are polynomials in :math:`\mathbf{r} = (\I, \x_0, \p_0, \x_1, \p_1, \ldots)`.
        This also means it maps :math:`\text{span}(\mathbf{r})` into itself:

        .. math:: U^\dagger \mathbf{r}_i U = \sum_j \tilde{U}_{ij} \mathbf{r}_j

        For Gaussian CV gates, this method returns the transformation matrix for
        the current parameter values of the Operation. The method is not defined
        for non-Gaussian (and non-CV) gates.

        Args:
            wire_order (Wires): global wire order defining which subspace the operator acts on
            inverse  (bool): if True, return the inverse transformation instead

        Raises:
            RuntimeError: if the specified operation is not Gaussian or is missing the `_heisenberg_rep` method

        Returns:
            array[float]: :math:`\tilde{U}`, the Heisenberg picture representation of the linear transformation
        """
        p = [qml.math.toarray(a) for a in self.parameters]
        if inverse:
            try:
                # TODO: expand this for the new par domain class, for non-unitary matrices.
                p[0] = np.linalg.inv(p[0])
            except np.linalg.LinAlgError:
                p[0] = -p[0]  # negate first parameter
        U = self._heisenberg_rep(p)  # pylint: disable=assignment-from-none

        # not defined?
        if U is None:
            raise RuntimeError(
                f"{self.name} is not a Gaussian operation, or is missing the _heisenberg_rep method."
            )

        return self.heisenberg_expand(U, wire_order)


class CVObservable(CV, Observable):
    r"""Base class for continuous-variable observables.

    The class attribute :attr:`~.ev_order` can be defined to indicate
    to PennyLane whether the corresponding CV observable is a polynomial in the
    quadrature operators. If so,

    * ``ev_order = 1`` indicates a first order polynomial in quadrature
      operators :math:`(\x, \p)`.

    * ``ev_order = 2`` indicates a second order polynomial in quadrature
      operators :math:`(\x, \p)`.

    If :attr:`~.ev_order` is not ``None``, then the Heisenberg representation
    of the observable should be defined in the static method :meth:`~.CV._heisenberg_rep`,
    returning an array of the correct dimension.
    """
    # pylint: disable=abstract-method
    ev_order = None  #: None, int: if not None, the observable is a polynomial of the given order in `(x, p)`.

    def heisenberg_obs(self, wire_order):
        r"""Representation of the observable in the position/momentum operator basis.

        Returns the expansion :math:`q` of the observable, :math:`Q`, in the
        basis :math:`\mathbf{r} = (\I, \x_0, \p_0, \x_1, \p_1, \ldots)`.

        * For first-order observables returns a real vector such
          that :math:`Q = \sum_i q_i \mathbf{r}_i`.

        * For second-order observables returns a real symmetric matrix
          such that :math:`Q = \sum_{ij} q_{ij} \mathbf{r}_i \mathbf{r}_j`.

        Args:
            wire_order (Wires): global wire order defining which subspace the operator acts on
        Returns:
            array[float]: :math:`q`
        """
        p = self.parameters
        U = self._heisenberg_rep(p)  # pylint: disable=assignment-from-none
        return self.heisenberg_expand(U, wire_order)


def operation_derivative(operation) -> np.ndarray:
    r"""Calculate the derivative of an operation.

    For an operation :math:`e^{i \hat{H} \phi t}`, this function returns the matrix representation
    in the standard basis of its derivative with respect to :math:`t`, i.e.,

    .. math:: \frac{d \, e^{i \hat{H} \phi t}}{dt} = i \phi \hat{H} e^{i \hat{H} \phi t},

    where :math:`\phi` is a real constant.

    Args:
        operation (.Operation): The operation to be differentiated.

    Returns:
        array: the derivative of the operation as a matrix in the standard basis

    Raises:
        ValueError: if the operation does not have a generator or is not composed of a single
            trainable parameter
    """
    generator, prefactor = qml.utils.get_generator(operation, return_matrix=True)
    return 1j * prefactor * generator @ operation.matrix()


@qml.BooleanFn
def not_tape(obj):
    """Returns ``True`` if the object is not a quantum tape"""
    return isinstance(obj, qml.tape.QuantumTape)


@qml.BooleanFn
def has_gen(obj):
    """Returns ``True`` if an operator has a generator defined."""
    try:
        obj.generator()
    except (AttributeError, OperatorPropertyUndefined, GeneratorUndefinedError):
        return False

    return True


@qml.BooleanFn
def has_grad_method(obj):
    """Returns ``True`` if an operator has a grad_method defined."""
    return obj.grad_method is not None


@qml.BooleanFn
def has_multipar(obj):
    """Returns ``True`` if an operator has more than one parameter
    according to ``num_params``."""
    return obj.num_params > 1


@qml.BooleanFn
def has_nopar(obj):
    """Returns ``True`` if an operator has no parameters
    according to ``num_params``."""
    return obj.num_params == 0


@qml.BooleanFn
def has_unitary_gen(obj):
    """Returns ``True`` if an operator has a unitary_generator
    according to the ``has_unitary_generator`` flag."""
    return obj in qml.ops.qubit.attributes.has_unitary_generator


@qml.BooleanFn
def is_measurement(obj):
    """Returns ``True`` if an operator is a ``MeasurementProcess`` instance."""
    return isinstance(obj, qml.measure.MeasurementProcess)


@qml.BooleanFn
def is_trainable(obj):
    """Returns ``True`` if any of the parameters of an operator is trainable
    according to ``qml.math.requires_grad``."""
    return any(qml.math.requires_grad(p) for p in obj.parameters)


@qml.BooleanFn
def defines_diagonalizing_gates(obj):
    """Returns ``True`` if an operator defines the diagonalizing
    gates are defined.

    This helper function is useful if the property is to be checked in
    a queuing context, but the resulting gates must not be queued.
    """

    with qml.tape.stop_recording():
        try:
            obj.diagonalizing_gates()
        except DiagGatesUndefinedError:
            return False
        return True
