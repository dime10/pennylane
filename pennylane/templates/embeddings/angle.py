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
r"""
Contains the ``AngleEmbedding`` template.
"""
# pylint: disable-msg=too-many-branches,too-many-arguments,protected-access
import pennylane as qml
from pennylane.ops import RX, RY, RZ
from pennylane.operation import Operation, AnyWires

ROT = {"X": RX, "Y": RY, "Z": RZ}


class AngleEmbedding(Operation):
    r"""
    Encodes :math:`N` features into the rotation angles of :math:`n` qubits, where :math:`N \leq n`.

    The rotations can be chosen as either :class:`~pennylane.ops.RX`, :class:`~pennylane.ops.RY`
    or :class:`~pennylane.ops.RZ` gates, as defined by the ``rotation`` parameter:

    * ``rotation='X'`` uses the features as angles of RX rotations

    * ``rotation='Y'`` uses the features as angles of RY rotations

    * ``rotation='Z'`` uses the features as angles of RZ rotations

    The length of ``features`` has to be smaller or equal to the number of qubits. If there are fewer entries in
    ``features`` than rotations, the circuit does not apply the remaining rotation gates.

    Args:
        features (tensor_like): input tensor of shape ``(N,)``, where N is the number of input features to embed,
            with :math:`N\leq n`
        wires (Any or Iterable[Any]): wires that the template acts on
        rotation (str): type of rotations used

    Example:

        Angle embedding encodes the features by using the specified rotation operation.

        .. code-block:: python

            dev = qml.device('default.qubit', wires=3)

            @qml.qnode(dev)
            def circuit(feature_vector):
                qml.AngleEmbedding(features=feature_vector, wires=range(3), rotation='Z')
                qml.Hadamard(0)
                return qml.probs(wires=range(3))

            X = [1,2,3]

        Here, we have also used rotation angles :class:`RZ`. If not specified, :class:`RX` is used as default.
        The resulting circuit is:

        >>> print(qml.draw(circuit)(X))
            0: ──RZ(1)──H──╭┤ Probs
            1: ──RZ(2)─────├┤ Probs
            2: ──RZ(3)─────╰┤ Probs

    """

    num_wires = AnyWires
    grad_method = None

    def __init__(self, features, wires, rotation="X", do_queue=True, id=None):

        if rotation not in ROT:
            raise ValueError(f"Rotation option {rotation} not recognized.")

        shape = qml.math.shape(features)[-1:]
        n_features = shape[0]
        if n_features > len(wires):
            raise ValueError(
                f"Features must be of length {len(wires)} or less; got length {n_features}."
            )

        self._hyperparameters = {
            "rotation": ROT[rotation]
        }

        wires = wires[:n_features]
        super().__init__(features, wires=wires, do_queue=do_queue, id=id)

    @property
    def num_params(self):
        return 1

    @staticmethod
    def compute_decomposition(features, wires, rotation):  # pylint: disable=arguments-differ
        r"""Compute a decomposition of this operator.

        The decomposition defines an Operator as a product of more fundamental gates:

        .. math:: O = O_1 O_2 \dots O_n.

        ``compute_decomposition`` is a static method and can provide the decomposition of a given
        operator without creating a specific instance.

        See also :meth:`~.AngleEmbedding.decomposition`.

        Args:
            features (tensor_like): input tensor of dimension ``(len(wires),)``
            wires (Any or Iterable[Any]): wires that the operator acts on
            rotation (~.Operator): rotation gate class

        Returns:
            list[~.Operator]: decomposition of the Operator into lower-level operations

        **Example**

        >>> features = torch.tensor([1., 2.])
        >>> qml.AngleEmbedding.compute_decomposition(features, wires=["a", "b"])
        XXX
        """
        batched = len(qml.math.shape(features)) > 1
        features = features.T if batched else features

        return [rotation(features[i], wires=wires[i]) for i in range(len(wires))]
