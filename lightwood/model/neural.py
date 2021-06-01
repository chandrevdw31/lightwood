from typing import List
import pandas as pd
from torch.nn.modules.loss import MSELoss
from lightwood.api import dtype
from lightwood.data.encoded_ds import ConcatedEncodedDs, EncodedDs
import time
from torch import nn
import torch
import numpy as np
from torch.utils.data import DataLoader
from torch.cuda.amp import GradScaler
from lightwood.api.types import LightwoodConfig
from lightwood.helpers.log import log
from lightwood.model.base import BaseModel
from lightwood.helpers.torch import LightwoodAutocast
from lightwood.model.helpers.default_net import DefaultNet
from lightwood.model.helpers.ranger import Ranger
from lightwood.model.helpers.transform_corss_entropy_loss import TransformCrossEntropyLoss
from torch.optim.optimizer import Optimizer


class Neural(BaseModel):
    model: DefaultNet

    def __init__(self, lightwood_config: LightwoodConfig):
        super().__init__(lightwood_config)
        self.model = None

    def _select_criterion(self) -> torch.nn.Module:
        if self.lightwood_config.output.data_dtype in (dtype.categorical, dtype.binary):
            criterion = TransformCrossEntropyLoss()
        elif self.lightwood_config.output.data_dtype in (dtype.tags):
            criterion = nn.BCEWithLogitsLoss()
        elif self.lightwood_config.output.data_dtype in (dtype.integer, dtype.float) and self.lightwood_config.problem_definition.timeseries_settings.is_timeseries:
            criterion = nn.L1Loss()
        elif self.lightwood_config.output.data_dtype in (dtype.integer, dtype.float):
            criterion = MSELoss()
        else:
            criterion = MSELoss()

        return criterion

    def _select_optimizer(self) -> Optimizer:
        if self.lightwood_config.problem_definition.timeseries_settings.is_timeseries:
            optimizer = Ranger(self.net.params(), lr=0.0005)
        else:
            optimizer = Ranger(self.net.params(), lr=0.0005, weight_decay=2e-2)

        return optimizer
    
    def _run_epoch(self, train_dl, criterion, optimizer, scaler) -> float:
        self.model = self.model.train()
        running_losses: List[float] = []
        for X, Y in train_dl:
            X = X.to(self.net.device)
            Y = Y.to(self.net.device)
            with LightwoodAutocast():
                Yh = self.net(X)
                loss = criterion(Yh, Y)
                if LightwoodAutocast.active:
                    scaler.scale(loss).backward()
                    scaler.step(self.optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    optimizer.step()
                optimizer.zero_grad()
            running_losses.append(loss.item())
        return np.mean(running_losses)
    
    def _error(self, test_dl, criterion) -> float:
        self.model = self.model.eval()
        running_losses: List[float] = []
        for X, Y in test_dl:
            X = X.to(self.net.device)
            Y = Y.to(self.net.device)
            Yh = self.net(X)
            running_losses.append(criterion(Yh, Y).item())
        return np.mean(running_losses)
            
    def fit(self, ds_arr: List[EncodedDs]) -> None:
        train_ds = ConcatedEncodedDs(ds_arr[0:-1])
        test_ds = ConcatedEncodedDs(ds_arr[-1:])

        self.model = self.DefaultNet(
            input_size=len(train_ds[0][0]),
            output_size=len(train_ds[0][1])
        )
        
        criterion = self._select_criterion()
        optimizer = self._select_optimizer()

        started = time.time()
        scaler = GradScaler()
        train_dl = DataLoader(train_ds, batch_size=200)
        test_dl = DataLoader(test_ds, batch_size=200)

        running_errors: List[float] = []
        for epoch in range(1e10):
            error = self._run_epoch(self, train_dl, criterion, optimizer, scaler)
            log.info(f'Training error of {error} during iteration {epoch}')

            running_errors.append(self._error(test_dl, criterion))
            if time.time() - started > self.lightwood_config.problem_definition.seconds_per_model:
                break

            if len(running_errors) > 10 and np.mean(running_errors[-5:]) < error:
                break
        
        # Do a single training run on the test data as well
        self._run_epoch(self, test_dl, criterion, optimizer, scaler)

    def __call__(self, ds: EncodedDs) -> pd.DataFrame:
        self.model = self.model.eval()
        decoded_predictions: List[object] = []
        for X, Y in ds:
            X = X.to(self.net.device)
            Y = Y.to(self.net.device)
            Yh = self.model(X)
            decoded_prediction = ds.decode_prediction(Yh)
            decoded_predictions.append(decoded_prediction)

        ydf = pd.DataFrame({'predictions': decoded_predictions})
        return ydf
