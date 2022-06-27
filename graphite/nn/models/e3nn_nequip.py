import torch
from torch.nn   import Embedding, ModuleList

from e3nn       import o3
from e3nn.nn    import Gate
from e3nn.math  import soft_one_hot_linspace
from ..conv.e3nn_nequip_interaction import E3NN_Interaction


def tp_path_exists(irreps_in1, irreps_in2, ir_out):
    irreps_in1 = o3.Irreps(irreps_in1).simplify()
    irreps_in2 = o3.Irreps(irreps_in2).simplify()
    ir_out = o3.Irrep(ir_out)

    for _, ir1 in irreps_in1:
        for _, ir2 in irreps_in2:
            if ir_out in ir1 * ir2:
                return True
    return False


class Compose(torch.nn.Module):
    def __init__(self, first, second):
        super().__init__()
        self.first = first
        self.second = second
        self.irreps_in = self.first.irreps_in
        self.irreps_out = self.second.irreps_out

    def forward(self, *input):
        x = self.first(*input)
        return self.second(x)


class E3NN_NequIP(torch.nn.Module):
    """NequIP model from https://arxiv.org/pdf/2101.03164.pdf.

    Args:
        irreps_in (e3nn.o3.Irreps or str): Irreps of input node features.
        irreps_hidden (e3nn.o3.Irreps or str): Irreps of node features at hidden layers.
        irreps_out (e3nn.o3.Irreps or str): Irreps of output node features.
        irreps_node (e3nn.o3.Irreps or str): Irreps of node attributes (not updated throughout model).
        irreps_edge (e3nn.o3.Irreps or str): Irreps of spherical_harmonics.
        num_convs (int): Number of interaction/conv layers. Must be more than 1.
        radial_neurons (list of ints): Number neurons per layers in the fully connected network.
            For first and hidden layers, not the output layer.
        num_species (int): Number of elements/species in the atomic data.
        max_radius (float): Cutoff radius used during graph construction.
        num_neighbors (float): Typical or average node degree (used for normalization).
    """
    def __init__(self,
        irreps_in      = '64x0e',
        irreps_hidden  = '128x0e + 16x1e',
        irreps_out     = '4x0e',
        irreps_node    = '1x0e',
        irreps_edge    = '1x0e + 1x1e + 1x2e',
        num_convs      = 3,
        radial_neurons = [16, 64],
        num_species    = 1,
        max_radius     = 3.15,
        num_neighbors  = 12,
    ):
        super().__init__()

        self.irreps_in      = o3.Irreps(irreps_in)
        self.irreps_hidden  = o3.Irreps(irreps_hidden)
        self.irreps_out     = o3.Irreps(irreps_out)
        self.irreps_node    = o3.Irreps(irreps_node)
        self.irreps_edge    = o3.Irreps(irreps_edge)
        self.num_convs      = num_convs
        self.max_radius     = max_radius
        self.num_edge_basis = radial_neurons[0]
        self.node_embedding = Embedding(num_species, self.irreps_in[0][0])

        act_scalars = {1: torch.nn.functional.silu, -1: torch.tanh}
        act_gates   = {1: torch.sigmoid, -1: torch.tanh}

        irreps = self.irreps_in
        self.interactions = ModuleList()
        for _ in range(num_convs-1):
            irreps_scalars = o3.Irreps([(m, ir) for m, ir in self.irreps_hidden if ir.l == 0 and tp_path_exists(irreps, self.irreps_edge, ir)])
            irreps_gated   = o3.Irreps([(m, ir) for m, ir in self.irreps_hidden if ir.l > 0  and tp_path_exists(irreps, self.irreps_edge, ir)])

            if irreps_gated.dim > 0:
                if tp_path_exists(irreps_node, self.irreps_edge, "0e"):
                    ir = "0e"
                elif tp_path_exists(irreps_node, self.irreps_edge, "0o"):
                    ir = "0o"
                else:
                    raise ValueError(f"irreps={irreps} times irreps_edge={self.irreps_edge} is unable to produce gates needed for irreps_gated={irreps_gated}.")
            else:
                ir = None
            irreps_gates = o3.Irreps([(mul, ir) for mul, _ in irreps_gated]).simplify()

            gate = Gate(
                irreps_scalars, [act_scalars[ir.p] for _, ir in irreps_scalars],  # scalar
                irreps_gates,   [act_gates[ir.p]   for _, ir in irreps_gates],  # gates (scalars)
                irreps_gated  # gated tensors
            )

            conv = E3NN_Interaction(
                irreps_in      = irreps,
                irreps_node    = self.irreps_node,
                irreps_edge    = self.irreps_edge,
                irreps_out     = gate.irreps_in,
                radial_neurons = radial_neurons,
                num_neighbors  = num_neighbors,
            )
            irreps = gate.irreps_out
            self.interactions.append(Compose(conv, gate))

        self.out = E3NN_Interaction(
            irreps_in      = irreps,
            irreps_node    = self.irreps_node,
            irreps_edge    = self.irreps_edge,
            irreps_out     = self.irreps_out,
            radial_neurons = radial_neurons,
            num_neighbors  = num_neighbors,
        )

    def forward(self, data):
        x, edge_index, edge_vec = data.x, data.edge_index, data.edge_attr
        if 'z' not in data:
            z = x.new_ones((x.shape[0], 1))

        h_atm = self.node_embedding(x)

        h_bnd = soft_one_hot_linspace(
            edge_vec.norm(dim=1),
            start  = 0.0,
            end    = self.max_radius,
            number = self.num_edge_basis,
            basis  = 'smooth_finite',
            cutoff = True,
        ).mul(self.num_edge_basis**0.5)

        edge_sh = o3.spherical_harmonics(self.irreps_edge, edge_vec, normalize=True, normalization='component')

        for layer in self.interactions:
            h_atm = layer(h_atm, z, edge_index, edge_sh, h_bnd)

        return self.out(h_atm, z, edge_index, edge_sh, h_bnd)
