import unittest

class TestBasic(unittest.TestCase):
    def test_0_predict_file_flow(self):
        from lightwood import generate_predictor
        from mindsdb_datasources import FileDS

        datasource = FileDS('https://raw.githubusercontent.com/mindsdb/benchmarks/main/datasets/adult_income/adult.csv')

        predictor_class_str = generate_predictor('income', datasource)
        print(f'Generated following predictor class: {predictor_class_str}')

        predictor_class = eval(predictor_class_str)
        print('Class was evaluated successfully')

        predictor = predictor_class()
        print('Class initialized successfully')

        predictor.learn(datasource)

        predictions = predictor.predict(datasource)
        print(predictions[0:100])
