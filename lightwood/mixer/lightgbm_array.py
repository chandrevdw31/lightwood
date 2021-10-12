from copy import deepcopy
from typing import Dict, List, Union

import numpy as np
import pandas as pd

from lightwood.api import dtype
from lightwood.helpers.log import log
from lightwood.mixer.base import BaseMixer
from lightwood.mixer.lightgbm import LightGBM
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class LightGBMArray(BaseMixer):
    """LightGBM-based model, intended for usage in time series tasks."""
    models: List[LightGBM]
    n_ts_predictions: int
    submodel_stop_after: float
    target: str
    supports_proba: bool

    def __init__(
            self, stop_after: int, target: str, dtype_dict: Dict[str, str],
            input_cols: List[str],
            n_ts_predictions: int, fit_on_dev: bool):
        super().__init__(stop_after)
        self.submodel_stop_after = stop_after / n_ts_predictions
        self.target = target
        dtype_dict[target] = dtype.float
        self.models = [LightGBM(self.submodel_stop_after, target, dtype_dict, input_cols, fit_on_dev, use_optuna=False)
                       for _ in range(n_ts_predictions)]
        self.n_ts_predictions = n_ts_predictions  # for time series tasks, how long is the forecast horizon
        self.supports_proba = False
        self.stable = True

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        log.info('Started fitting LGBM models for array prediction')

        for timestep in range(self.n_ts_predictions):
            if timestep > 0:
                train_data.data_frame[self.target] = train_data.data_frame[f'{self.target}_timestep_{timestep}']
                dev_data.data_frame[self.target] = dev_data.data_frame[f'{self.target}_timestep_{timestep}']

            self.models[timestep].fit(train_data, dev_data)  # @TODO: this call could be parallelized

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        log.info('Updating array of LGBM models...')

        for timestep in range(self.n_ts_predictions):
            if timestep > 0:
                train_data.data_frame[self.target] = train_data.data_frame[f'{self.target}_timestep_{timestep}']
                dev_data.data_frame[self.target] = dev_data.data_frame[f'{self.target}_timestep_{timestep}']

            self.models[timestep].partial_fit(train_data, dev_data)  # @TODO: this call could be parallelized

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        if args.predict_proba:
            log.warning('This model does not output probability estimates')

        length = sum(ds.encoded_ds_lenghts) if isinstance(ds, ConcatedEncodedDs) else len(ds)
        ydf = pd.DataFrame(0,  # zero-filled
                           index=np.arange(length),
                           columns=[f'prediction_{i}' for i in range(self.n_ts_predictions)])

        for timestep in range(self.n_ts_predictions):
            ydf[f'prediction_{timestep}'] = self.models[timestep](ds, args)

        ydf['prediction'] = ydf.values.tolist()
        return ydf[['prediction']]


class LightGBMArrayAR(LightGBM):
    """LightGBM-based model, intended for usage in time series tasks. It is trained for t+1 using all
    available data, then partially fit using its own output for longer time horizons."""
    n_ts_predictions: int
    target: str
    supports_proba: bool

    def __init__(self, stop_after: int, target: str, dtype_dict: Dict[str, str], ts_analysis: Dict,
                 input_cols: List[str], n_ts_predictions: int, fit_on_dev: bool,
                 use_optuna: bool = True, exhaustive_fitting: bool = False):
        super().__init__(stop_after, target, dtype_dict, input_cols, fit_on_dev, use_optuna=use_optuna)
        self.dtype_dict[target] = dtype.float
        self.n_ts_predictions = n_ts_predictions  # for time series tasks, how long is the forecast horizon
        self.exhaustive_fitting = exhaustive_fitting
        self.ts_analysis = ts_analysis
        self.supports_proba = False
        self.stable = False

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        log.info('Started fitting LGBM model for array prediction')
        log.info(f'Exhaustive mode: {"enabled" if self.exhaustive_fitting else "disabled"}')
        data = (train_data, dev_data)
        super().fit(*data)  # fit as normal

        if self.exhaustive_fitting:
            # fit t+n using t+(n-1) predictions as historical context
            use_optuna = self.use_optuna
            self.use_optuna = False

            original_dfs = [deepcopy(ds.data_frame) for ds in data]
            for timestep in range(1, self.n_ts_predictions):
                new_ds_arr = []
                for ds in data:
                    predictions = super().__call__(ds)
                    new_ds = self._displace_ds(ds, predictions, timestep)
                    new_ds_arr.append(new_ds)

                log.info(f'Fitting T+{timestep+1}')
                super().fit(*data)

            self.use_optuna = use_optuna

            # restore ds_arr dataframes' original states
            for i, ds in enumerate(data):
                ds.data_frame = original_dfs[i]

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        if args.predict_proba:
            log.warning('This model does not output probability estimates')

        # force list of EncodedDs, as ConcatedEncodedDs does not support modifying its dataframe
        ds_arr = ds.encoded_ds_arr if isinstance(ds, ConcatedEncodedDs) else [ds]
        original_dfs = [deepcopy(ds.data_frame) for ds in ds_arr]
        length = sum([len(d) for d in ds_arr])

        ydf = pd.DataFrame(0,
                           index=np.arange(length),
                           columns=[f'prediction_{i}' for i in range(self.n_ts_predictions)])

        for timestep in range(self.n_ts_predictions):
            all_predictions = []
            for i, ds in enumerate(ds_arr):
                predictions = super().__call__(ds, args)
                all_predictions.append(predictions)
                if timestep + 1 < self.n_ts_predictions:
                    ds_arr[i] = self._displace_ds(ds, predictions, timestep + 1)

            all_predictions = pd.concat(all_predictions).reset_index(drop=True)
            ydf[f'prediction_{timestep}'] = all_predictions

        # restore original dataframes (for correct model analysis)
        for i, ds in enumerate(ds_arr):
            ds.data_frame = original_dfs[i]

        ydf['prediction'] = ydf.values.tolist()
        return ydf[['prediction']]

    def _displace_ds(self, data: EncodedDs, predictions: pd.DataFrame, timestep: int):
        """Moves all array columns one timestep ahead, inserting the LGBM model predictions as historical context"""
        predictions.index = data.data_frame.index
        data.data_frame['__mdb_predictions'] = predictions

        # add prediction to history and displace order by column
        for idx, row in data.data_frame.iterrows():
            histcol = f'__mdb_ts_previous_{self.target}'
            if histcol in data.data_frame.columns:
                data.data_frame.at[idx, histcol] = row.get(histcol)[1:] + [row.get('__mdb_predictions')]

            for col in self.ts_analysis['tss'].order_by:
                if col in data.data_frame.columns:
                    deltas = self.ts_analysis['deltas']
                    group = frozenset(row[self.ts_analysis['tss'].group_by]) \
                        if self.ts_analysis['tss'].group_by \
                        else '__default'  # used w/novel group

                    delta = deltas.get(group, deltas['__default'])[col]
                    data.data_frame.at[idx, col] = row.get(col)[1:] + [row.get(col)[-1] + delta]

        # change target if training
        if f'{self.target}_timestep_{timestep}' in data.data_frame.columns:
            data.data_frame[self.target] = data.data_frame[f'{self.target}_timestep_{timestep}']

        data.data_frame.pop('__mdb_predictions')  # drop temporal column
        return data
