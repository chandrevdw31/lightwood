import inspect
import importlib
from itertools import product
from typing import Dict, Union

import optuna
import numpy as np
import pandas as pd
from sktime.forecasting.arima import AutoARIMA
from sktime.forecasting.trend import PolynomialTrendForecaster
from sktime.forecasting.compose import TransformedTargetForecaster
from sktime.transformations.series.detrend import ConditionalDeseasonalizer
from sktime.transformations.series.detrend import Detrender
from sktime.forecasting.base import ForecastingHorizon, BaseForecaster
from sktime.performance_metrics.forecasting import MeanAbsolutePercentageError

from lightwood.api import dtype
from lightwood.helpers.log import log
from lightwood.mixer.base import BaseMixer
from lightwood.api.types import PredictionArguments
from lightwood.helpers.general import get_group_matches
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class SkTime(BaseMixer):
    forecaster: str
    n_ts_predictions: int
    target: str
    supports_proba: bool
    model_path: str
    hyperparam_search: bool

    def __init__(
            self, stop_after: float, target: str, dtype_dict: Dict[str, str],
            n_ts_predictions: int, ts_analysis: Dict, model_path: str = 'arima.AutoARIMA', auto_size: bool = True,
            hyperparam_search: bool = False, target_transforms: Dict[str, Union[int, str]] = {}):
        """
        This mixer is a wrapper around the popular time series library sktime. It exhibits different behavior compared
        to other forecasting mixers, as it predicts based on indices in a forecasting horizon that is defined with
        respect to the last seen data point at training time.
        
        Due to this, the mixer tries to "fit_on_all" so that the latest point in the validation split marks the 
        difference between training data and where forecasts will start. In practice, you need to specify how much 
        time has passed since the aforementioned timestamp for correct forecasts. By default, it is assumed that
         predictions are for the very next timestamp post-training.
        
        If the task has groups (i.e. 'TimeseriesSettings.group_by' is not empty), the mixer will spawn one forecaster 
        object per each different group observed at training time, plus an additional default forecaster fit with all data.
        
        There is an optuna-based automatic hyperparameter search. For now, it considers selecting the forecaster type
        based on the global SMAPE error across all groups.
        
        :param stop_after: time budget in seconds.
        :param target: column to forecast.
        :param dtype_dict: dtypes of all columns in the data.
        :param n_ts_predictions: length of forecasted horizon.
        :param ts_analysis: dictionary with miscellaneous time series info, as generated by 'lightwood.data.timeseries_analyzer'.
        :param model_path: sktime forecaster to use as underlying model(s). Should be a string with format "$module.$class' where '$module' is inside `sktime.forecasting`. Default is 'arima.AutoARIMA'.
        :param hyperparam_search: bool that indicates whether to perform the hyperparameter tuning or not.
        :param auto_size: whether to filter out old data points if training split is bigger than a certain threshold (defined by the dataset sampling frequency). Enabled by default to avoid long training times in big datasets.
        :param target_transforms: arguments for target transformation. Currently supported format: {'detrender': int, 'deseasonalizer': 'add' | 'mul' }. 'detrender' forces a particular type of polynomial to fit as trend curve for the series, while 'deseasonalizer' specifies additive or multiplicative seasonality decomposition (only applied if a seasonality test is triggered). By default, both are disabled.
        """  # noqa
        super().__init__(stop_after)
        self.stable = True
        self.prepared = False
        self.supports_proba = False
        self.target = target
        dtype_dict[target] = dtype.float

        self.ts_analysis = ts_analysis
        self.n_ts_predictions = n_ts_predictions
        self.grouped_by = ['__default'] if not ts_analysis['tss'].group_by else ts_analysis['tss'].group_by
        self.auto_size = auto_size
        self.cutoff_factor = 4  # times the detected maximum seasonal period
        self.target_transforms = {
            'detrender': 0,          # degree of detrender polynomial (0: disabled)
            'deseasonalizer': ''  # seasonality decomposition: 'add'itive or 'mul'tiplicative (else, disabled)
        }
        self.target_transforms.update(target_transforms)

        # optuna hyperparameter tuning
        self.models = {}
        self.study = None
        self.hyperparam_dict = {}
        self.model_path = model_path
        self.hyperparam_search = hyperparam_search
        self.trial_error_fn = MeanAbsolutePercentageError(symmetric=True)
        self.possible_models = ['ets.AutoETS', 'theta.ThetaForecaster', 'arima.AutoARIMA']
        self.n_trials = len(self.possible_models)

        # sktime forecast horizon object is made relative to the end of the latest data point seen at training time
        # the default assumption is to forecast the next `self.n_ts_predictions` after said data point
        self.fh = ForecastingHorizon(np.arange(1, self.n_ts_predictions + 1), is_relative=True)

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Fits a set of sktime forecasters. The number of models depends on how many groups are observed at training time.

        Forecaster type can be specified by providing the `model_class` argument in `__init__()`. It can also be determined by hyperparameter optimization based on dev data validation error.
        """  # noqa
        log.info('Started fitting sktime forecaster for array prediction')

        if self.hyperparam_search:
            search_space = {'class': self.possible_models}
            self.study = optuna.create_study(direction='minimize', sampler=optuna.samplers.GridSampler(search_space))
            self.study.optimize(lambda trial: self._get_best_model(trial, train_data, dev_data), n_trials=self.n_trials)
            data = ConcatedEncodedDs([train_data, dev_data])
            self._fit(data)

        else:
            data = ConcatedEncodedDs([train_data, dev_data])
            self._fit(data)

    def _fit(self, data):
        """
        Internal method that fits forecasters to a given dataframe.
        """
        df = data.data_frame.sort_values(by=f'__mdb_original_{self.ts_analysis["tss"].order_by[0]}')
        data = {'data': df[self.target],
                'group_info': {gcol: df[gcol].tolist()
                               for gcol in self.grouped_by} if self.ts_analysis['tss'].group_by else {}}

        if not self.hyperparam_search and not self.study:
            module_name = self.model_path
        else:
            finished_study = sum([int(trial.state.is_finished()) for trial in self.study.trials]) == self.n_trials
            if finished_study:
                log.info(f'Selected best model: {self.study.best_params["class"]}')
                module_name = self.study.best_params['class']
            else:
                module_name = self.hyperparam_dict['class']  # select active optuna choice

        sktime_module = importlib.import_module('.'.join(['sktime', 'forecasting', module_name.split(".")[0]]))
        try:
            model_class = getattr(sktime_module, module_name.split(".")[1])
        except AttributeError:
            model_class = AutoARIMA  # use AutoARIMA when the provided class does not exist

        for group in self.ts_analysis['group_combinations']:
            kwargs = {}
            options = {
                'sp': self.ts_analysis['periods'].get(group, '__default'),  # seasonality period
                'suppress_warnings': True,                                  # ignore warnings if possible
                'error_action': 'raise',                                    # avoids fit() failing silently
            }

            for k, v in options.items():
                kwargs = self._add_forecaster_kwarg(model_class, kwargs, k, v)

            model_pipeline = [("forecaster", model_class(**kwargs))]

            trend_degree = self.target_transforms['detrender']
            seasonality_type = self.target_transforms['deseasonalizer']
            if seasonality_type in ('add', 'mul'):
                model_pipeline.insert(0, ("deseasonalizer",
                                          ConditionalDeseasonalizer(
                                              model='additive' if seasonality_type == 'add' else 'multiplicative',
                                              sp=options['sp']
                                          )))
            if trend_degree > 0:
                model_pipeline.insert(0, ("detrender",
                                          Detrender(forecaster=PolynomialTrendForecaster(degree=trend_degree))))

            self.models[group] = TransformedTargetForecaster(model_pipeline)

            if self.grouped_by == ['__default']:
                series_idxs = data['data'].index
                series_data = data['data'].values
            else:
                series_idxs, series_data = get_group_matches(data, group)

            if series_data.size > self.ts_analysis['tss'].window:
                series = pd.Series(series_data.squeeze(), index=series_idxs)
                series = series.sort_index(ascending=True)
                series = series.reset_index(drop=True)
                series = series.loc[~pd.isnull(series.values)]  # remove NaN  # @TODO: benchmark imputation vs this?

                # if data is huge, filter out old records for quicker fitting
                if self.auto_size:
                    cutoff = min(len(series), max(500, options['sp'] * self.cutoff_factor))
                    series = series.iloc[-cutoff:]
                try:
                    self.models[group].fit(series, fh=self.fh)
                except Exception:
                    self.models[group] = model_class()  # with default options (i.e. no seasonality, among others)
                    self.models[group].fit(series, fh=self.fh)

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Note: sktime asks for "specification of the time points for which forecasts are requested", and this mixer complies by assuming forecasts will start immediately after the last observed value.

        Because of this, `ProblemDefinition.fit_on_all` is set to True so that `partial_fit` uses both `dev` and `test` splits to fit the models.

        Due to how lightwood implements the `update` procedure, expected inputs for this method are:

        :param dev_data: original `test` split (used to validate and select model if ensemble is `BestOf`).
        :param train_data: concatenated original `train` and `dev` splits.
        """  # noqa
        self.hyperparam_search = False
        self.fit(dev_data, train_data)
        self.prepared = True

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        """
        Calls the mixer to emit forecasts.
        
        If there are groups that were not observed at training, a default forecaster (trained on all available data) is used, warning the user that performance might not be optimal.
        
        Latest data point in `train_data` passed to `fit()` determines the starting point for predictions. Relative offsets can be provided to forecast through the `args.forecast_offset` argument.
        """  # noqa
        if args.predict_proba:
            log.warning('This mixer does not output probability estimates')

        length = sum(ds.encoded_ds_lenghts) if isinstance(ds, ConcatedEncodedDs) else len(ds)
        ydf = pd.DataFrame(0,  # zero-filled
                           index=np.arange(length),
                           columns=['prediction'],
                           dtype=object)

        data = {'data': ds.data_frame[self.target],
                'group_info': {gcol: ds.data_frame[gcol].tolist()
                               for gcol in self.grouped_by} if self.ts_analysis['tss'].group_by else {}}

        pending_idxs = set(range(length))
        all_group_combinations = list(product(*[set(x) for x in data['group_info'].values()]))
        for group in all_group_combinations:
            series_idxs, series_data = get_group_matches(data, group)

            if series_data.size > 0:
                group = frozenset(group)
                series_idxs = sorted(series_idxs)
                if self.models.get(group, False) and self.models[group].is_fitted:
                    forecaster = self.models[group]
                else:
                    log.warning(f"Applying default forecaster for novel group {group}. Performance might not be optimal.")  # noqa
                    forecaster = self.models['__default']
                series = pd.Series(series_data.squeeze(), index=series_idxs)
                ydf = self._call_groupmodel(ydf, forecaster, series, offset=args.forecast_offset)
                pending_idxs -= set(series_idxs)

        # apply default model in all remaining novel-group rows
        if len(pending_idxs) > 0:
            series = pd.Series(data['data'][list(pending_idxs)].squeeze(), index=sorted(list(pending_idxs)))
            ydf = self._call_groupmodel(ydf, self.models['__default'], series, offset=args.forecast_offset)

        return ydf[['prediction']]

    def _call_groupmodel(self,
                         ydf: pd.DataFrame,
                         model: BaseForecaster,
                         series: pd.Series,
                         offset: int = 0):
        """
        Inner method that calls a `sktime.BaseForecaster`.

        :param offset: indicates relative offset to the latest data point seen during model training. Cannot be less than the number of training data points + the amount of diffences applied internally by the model.
        """  # noqa
        original_index = series.index
        series = series.reset_index(drop=True)

        if isinstance(model, TransformedTargetForecaster):
            submodel = model.steps_[-1][-1]
        else:
            submodel = model

        if hasattr(submodel, '_cutoff') and hasattr(submodel, 'd'):
            model_d = 0 if submodel.d is None else submodel.d
            min_offset = -submodel._cutoff + model_d + 1
        else:
            min_offset = -np.inf

        for idx, _ in enumerate(series.iteritems()):
            # displace by 1 according to sktime ForecastHorizon usage
            start_idx = max(1 + idx + offset, min_offset)
            end_idx = 1 + idx + offset + self.n_ts_predictions
            ydf['prediction'].iloc[original_index[idx]] = model.predict(np.arange(start_idx, end_idx)).tolist()

        return ydf

    def _get_best_model(self, trial, train_data, test_data):
        """
        Helper function for Optuna hyperparameter optimization.
        For now, it uses dev data split to find the best model out of the list specified in self.possible_models.
        """

        self.hyperparam_dict = {
            'class': trial.suggest_categorical('class', self.possible_models)
        }
        log.info(f'Starting trial with hyperparameters: {self.hyperparam_dict}')
        try:
            self._fit(train_data)
            y_true = test_data.data_frame[self.target].values[:self.n_ts_predictions]
            y_pred = self(test_data)['prediction'].iloc[0][:len(y_true)]
            error = self.trial_error_fn(y_true, y_pred)
        except Exception as e:
            log.debug(e)
            error = np.inf

        log.info(f'Trial got error: {error}')
        return error

    def _add_forecaster_kwarg(self, forecaster: BaseForecaster, kwargs: dict, key: str, value):
        """
        Adds arguments to the `kwargs` dictionary if the key-value pair is valid for the `forecaster` class signature.
        """
        if key in [p.name for p in inspect.signature(forecaster).parameters.values()]:
            kwargs[key] = value

        return kwargs
