# Copyright 2018-2022 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

import pennylane as qml
from pennylane import numpy as np
from pennylane.ops.op_math import SymbolicOp


class TempOperator(qml.operation.Operator):
    num_wires = 1


def test_intialization(self):
    """Test initialization for a SymbolicOp"""
    base = TempOperator("a")

    op = SymbolicOp(base, id="something")

    assert op.base is base
    assert op.hyperparameters["base"] is base
    assert op.id == "something"
    assert op.queue_idx is None
    assert op.name == "Symbolic"


class TestProperties:
    """Test the properties of the symbolic op."""

    def test_data(self):
        """Test that the data property for symbolic ops allows for the getting
        and setting of the base operator's data."""
        x = np.array(1.234)

        base = TempOperator(x, "a")
        op = SymbolicOp(base)

        assert op.data == [x]

        # update parameters through op
        x_new = np.array(2.345)
        op.data = [x_new]
        assert base.data == [x_new]
        assert op.data == [x_new]

        # update base data updates symbolic data
        x_new2 = np.array(3.45)
        base.data = [x_new2]
        assert op.data == [x_new2]

    def test_parameters(self):
        """Test parameter property is a list of the base's trainable parameters."""
        x = np.array(9.876)
        base = TempOperator(x, "b")
        op = SymbolicOp(base)
        assert op.parameters == [x]

    def test_num_params(self):
        """Test symbolic ops defer num-params to those of the base operator."""
        base = TempOperator(1.234, 3.432, 0.5490, 8.789453, wires="b")
        op = SymbolicOp(base)

        assert op.num_params == base.num_params == 4

    @pytest.mark.parametrize("has_mat", (True, False))
    def test_has_matrix(self, has_mat):
        """Test that a symbolic op has a matrix if its base has a matrix."""

        class DummyOp(qml.operation.Operator):
            num_wires = 1
            has_matrix = has_mat

        base = DummyOp("b")
        op = SymbolicOp(base)
        assert op.has_matrix == has_mat

    @pytest.mark.parametrize("is_herm", (True, False))
    def test_is_hermitian(self, is_herm):
        """Test that symbolic op is hermitian if the base is hermitian."""

        class DummyOp(qml.operation.Operator):
            num_wires = 1
            is_hermitian = is_herm

        base = DummyOp("b")
        op = SymbolicOp(base)
        assert op.is_hermitian == is_herm

    @pytest.mark.parametrize("queue_cat", ("_ops", "_prep", None))
    def test_queuecateory(self, queue_cat):
        """Test that a symbolic operator inherits the queue_category from its base."""

        class DummyOp(qml.operation.Operator):
            num_wires = 1
            _queue_category = queue_cat

        op = SymbolicOp(DummyOp("b"))
        assert op._queue_category == queue_cat

    def test_private_wires_getter(self):
        """Test that wires can be accessed via the private `_wires` property."""
        w = qml.wires.Wires("a")
        base = TempOperator(w)
        op = SymbolicOp(base)
        assert op._wires == base._wires == w

    def test_private_wires_setter(self):
        """Test that base wires can be set through the operator's private `_wires` property."""
        w = qml.wires.Wires("a")
        base = TempOperator(w)
        op = SymbolicOp(base)

        w2 = qml.wires.Wires("c")
        op._wires = w2

        assert op._wires == base._wires == w2

    def test_num_wires(self):
        """Test that the number of wires is the length of the `wires` property, rather
        than the `num_wires` set by the base."""

        class DummyOp(qml.operation.Operator):
            num_wires = qml.operation.AnyWires

        t = DummyOp(wires=(0, 1, 2))
        op = SymbolicOp(t)
        assert op.num_wires == 3

    def test_batching_properties(self):
        """Test a symbolic operator inherits the batching properties of its base."""

        class DummyOp(qml.operation.Operator):
            ndim_params = (0, 2)
            num_wires = 1

        param1 = [0.3] * 3
        param2 = [[[0.3, 1.2]]] * 3

        base = DummyOp(param1, param2, wires=0)
        op = SymbolicOp(base)

        assert op.ndim_params == (0, 2)
        assert op.batch_size == 3


class TestQueuing:
    """Test that Symbolic Operators queue and update base metadata."""

    def test_queuing(self):
        """Test symbolic op queues and updates base metadata."""
        with qml.tape.QuantumTape() as tape:
            base = TempOperator("a")
            op = SymbolicOp(base)

        assert tape._queue[base]["owner"] is op
        assert tape._queue[op]["owns"] is base
        assert tape.operations == [op]

    def test_queuing_base_defined_outside(self):
        """Test symbolic op queues without adding base to the queue if it isn't already in the queue."""

        base = TempOperator("b")
        with qml.tape.QuantumTape() as tape:
            op = SymbolicOp(base)

        assert len(tape._queue) == 1
        assert tape._queue[op]["owns"] is base
        assert tape.operations == [op]

    def test_do_queue_false(self):
        """Test that queuing can be avoided if `do_queue=False`."""

        base = TempOperator("c")
        with qml.tape.QuantumTape() as tape:
            op = SymbolicOp(base, do_queue=False)

        assert len(tape.queue) == 0
