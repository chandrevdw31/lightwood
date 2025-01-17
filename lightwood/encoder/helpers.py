import torch
import numpy as np
from sklearn.preprocessing import MinMaxScaler, OneHotEncoder, OrdinalEncoder


class MinMaxNormalizer:
    def __init__(self, combination=()):
        self.scaler = MinMaxScaler()
        self.abs_mean = None
        self.combination = combination  # tuple with values in grouped-by columns
        self.output_size = 1

    def prepare(self, x: np.ndarray) -> None:
        # @TODO: streamline input type
        if isinstance(x[0], list):
            x = np.vstack(x)
        if isinstance(x[0], torch.Tensor):
            x = torch.stack(x).numpy()
        if len(x.shape) < 2:
            x = np.expand_dims(x, axis=1)

        x = x.astype(float)
        x[x == None] = 0 # noqa
        self.abs_mean = np.mean(np.abs(x))
        self.scaler.fit(x)

    def encode(self, y: np.ndarray) -> torch.Tensor:
        if isinstance(y[0], list):
            y = np.vstack(y)
        if isinstance(y[0], torch.Tensor):
            y = torch.stack(y).numpy()
        if len(y.shape) < 2:
            y = np.expand_dims(y, axis=1)

        shape = y.shape
        y = y.astype(float).reshape(-1, self.scaler.n_features_in_)
        out = torch.reshape(torch.Tensor(self.scaler.transform(y)), shape)
        return out

    def decode(self, y):
        return self.scaler.inverse_transform(y)


class CatNormalizer:
    def __init__(self, encoder_class='one_hot'):
        self.encoder_class = encoder_class
        if encoder_class == 'one_hot':
            self.scaler = OneHotEncoder(sparse=False, handle_unknown='ignore')
        else:
            self.scaler = OrdinalEncoder()

        self.unk = "<UNK>"

    def prepare(self, x):
        X = []
        for i in x:
            for j in i:
                X.append(j if j is not None else self.unk)
        self.scaler.fit(np.array(X).reshape(-1, 1))
        self.output_size = len(self.scaler.categories_[0]) if self.encoder_class == 'one_hot' else 1

    def encode(self, Y):
        y = np.array([[str(j) if j is not None else self.unk for j in i] for i in Y])
        out = []
        for i in y:
            transformed = self.scaler.transform(i.reshape(-1, 1))
            if isinstance(self.scaler, OrdinalEncoder):
                transformed = transformed.flatten()
            out.append(transformed)

        return torch.Tensor(out)

    def decode(self, y):
        return [[i[0] for i in self.scaler.inverse_transform(o)] for o in y]
