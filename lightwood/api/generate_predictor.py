import lightwood
from lightwood.api import LightwoodConfig
from mindsdb_datasources import DataSource
import torch
import numpy as np
import random
import pandas as pd


def generate_predictor_code(lightwood_config: LightwoodConfig) -> str:
	print(f'\n\n{repr(lightwood_config)}\n\n')
	feature_code_arr = []
	for feature in lightwood_config.features.values():
		feature_code_arr.append(f"""'{feature.name}':{feature.encoder}""")

	encoder_code = '{\n' + '\n,'.join(feature_code_arr) + '\n}'
	import_code = '\n'.join(lightwood_config.imports)

	return f"""
{import_code}
import pandas as pd


class Predictor():
	def __init__(self):
		self.seed()
		self.target = {lightwood_config.output.name}

	def seed(self):
		torch.manual_seed(66)
		torch.backends.cudnn.deterministic = True
		torch.backends.cudnn.benchmark = False
		np.random.seed(66)
		random.seed(66)

	def learn(self, data: DataSource) -> None:
		# Build a Graph from the JSON
		# Using eval is a bit ugly and we could replace it with factories, personally I'm against this, as it ads pointless complexity
		self.encoders = {encoder_code}

		self.model = self.lightwood_config['output']['model']

		# Do all the trainining and the data cleaning/processing
		data = {lightwood_config.cleaner}(data)
		data = {lightwood_config.splitter}(data)
		nfolds = len(data)

		for encoder in self.encoders.values():
			if encoder.uses_folds:
				encoder.prepare(data[0:nfolds])
			else:
				encoder.prepare(pd.concat(data[0:nfolds]))

		encoded_data = lightwood.encode(self.encoders, data)

		self.models = {lightwood_config.output.models}
		for model in self.models:
			model.fit(encoded_data[0:nfolds], data[0:nfolds])

		self.ensemble = {lightwood_config.output.ensemble}(self.models, encoded_data[nfolds], data[nfolds])

		self.confidence_model, self.predictor_analysis = {lightwood_config.analyzer}(self.ensemble, encoded_data[nfolds], data[nfolds])

	def predict(self, data: DataSource) -> pd.DataFrame:
		encoded_data = lightwood.encode(self.encoders, data)
		df = self.ensemble(encoded_data)
		return df
	"""

def config_from_data(target: str, data: DataSource) -> None:
	type_information = lightwood.data.infer_types(data)
	statistical_analysis = lightwood.data.statistical_analysis(data, type_information)
	lightwood_config = lightwood.generate_config(target, type_information=type_information, statistical_analysis=statistical_analysis)
	return lightwood_config

def generate_predictor(target: str=None, datasource: DataSource=None, lightwood_config: LightwoodConfig=None) -> str:
	if lightwood_config is None:
		lightwood_config = config_from_data(target, datasource)

	predictor_code = generate_predictor_code(lightwood_config)
	return predictor_code