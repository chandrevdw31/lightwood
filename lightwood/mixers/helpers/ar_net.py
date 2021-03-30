import torch
from torch import nn
from lightwood.mixers.helpers.default_net import DefaultNet
from lightwood.helpers.torch import LightwoodAutocast
from itertools import chain


class ArNet(DefaultNet):
    def __init__(self, dynamic_parameters,
                 transformer,
                 input_size=None,
                 output_size=None,
                 nr_outputs=None,
                 shape=None,
                 max_params=3e5,
                 dropout=None,
                 pretrained_net=None):
        """
        :param data_info: lightwood.mixers.helpers.transformer.Transformer; used to save the indexes for which we learn
        the autoregressive branch of the network.
        """
        assert len(transformer.output_features) == 1  # ArNet supports single target prediction only
        super().__init__(dynamic_parameters,
                         input_size=input_size,
                         output_size=output_size,
                         nr_outputs=nr_outputs,
                         shape=shape,
                         max_params=max_params,
                         dropout=dropout,
                         pretrained_net=pretrained_net
                         )
        target = transformer.output_features[0]
        self.transformer = transformer
        self.ar_column = f'__mdb_ts_previous_{target}'
        self.ar_idxs = list(*[transformer.input_idxs[col]
                              for col in transformer.input_idxs
                              if col == self.ar_column])

        # TODO: custom initialization, exponential between 0 and 1 to favour most recent value
        dims = [# (len(self.ar_idxs), len(self.ar_idxs)),
                # (len(self.ar_idxs), len(self.ar_idxs)),
                # (len(self.ar_idxs), len(self.ar_idxs)),
                (len(self.ar_idxs), transformer.feature_len_map[target])
                ]
        linears = [nn.Linear(in_features=inf, out_features=outf) for inf, outf in dims]
        self.ar_net = nn.Sequential(*linears)  # nn.Linear(in_features=len(self.ar_idxs),
                                               # out_features=transformer.feature_len_map[target])
        self.ar_net.to(self.device)

    def to(self, device=None, available_devices=None):
        self.ar_net.to(device)
        return super().to(device, available_devices)

    def forward(self, input):
        """
        In this particular model, we just need to forward the network defined in setup, with our input
        :param input: a pytorch tensor with the input data of a batch
        :return: output of the network
        """
        with LightwoodAutocast():
            residual_output = self._foward_net(input)
            ar_output = self.ar_net(input[:, self.ar_idxs])
            # vector[0] = 1 if real < 0 and not self.positive_domain else 0
            # vector[1] = math.log(abs(real)) if abs(real) > 0 else -20
            # vector[2] = real / mean
            # force unit root
            if self.ar_net.training:
                self.ar_net._modules['0'].weight = nn.Parameter(torch.clamp(self.ar_net._modules['0'].weight,
                                                                            0.0,
                                                                            0.999))

        return ar_output + residual_output# 0.0*