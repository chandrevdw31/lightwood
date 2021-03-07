"""
2021.03.07
TODO:
Freeze base_model in DistilBertModel
add more complicated layer and then build a model.

Pre-trained embedding model.

This deploys a hugging face transformer
and trains for a few epochs onto the target.

Once this has been completed, it provides embedding
using the updated transformer embedding.

NOTE - GPT2 does NOT have a padding token!!

Currently max_len doesn't do anything.
"""

from functools import partial

import torch
from torch.utils.data import DataLoader

from lightwood.encoders.text.helpers.transformer_helpers import TextEmbed

from lightwood.constants.lightwood import COLUMN_DATA_TYPES
from lightwood.helpers.device import get_devices
from lightwood.encoders.encoder_base import BaseEncoder
from lightwood.logger import log

from transformers import (
    DistilBertModel,
    DistilBertForSequenceClassification,
    DistilBertTokenizerFast,
    # DistilBertConfig,
    AlbertModel,
    AlbertForSequenceClassification,
    AlbertTokenizerFast,
    # AlbertConfig,
    GPT2Model,
    GPT2ForSequenceClassification,
    GPT2TokenizerFast,
    # GPT2Config,
    BartModel,
    BartForSequenceClassification,
    BartTokenizerFast,
    # BartConfig,
    AdamW,
    get_linear_schedule_with_warmup,
)


class PretrainedLang(BaseEncoder):
    """
    Pretrained language models.
    Option to train on a target encoding of choice.

    The "sent_embedder" parameter refers to a function to make
    sentence embeddings, given a 1 x N_tokens x N_embed input

    Args:
    is_target ::Bool; data column is the target of ML.
    model_name ::str; name of pre-trained model
    desired_error ::float
    max_training_time ::int; seconds to train
    custom_tokenizer ::function; custom tokenizing function
    sent_embedder ::str; make a sentence embedding from seq of word embeddings
                         default, sum all tokens and average
    batch_size  ::int; size of batfch
    max_position_embeddings ::int; max sequence length
    custom_train ::Bool; whether to train text on target or not.
    frozen ::Bool; whether to freeze tranformer and train a linear layer head
    """

    def __init__(
        self,
        is_target=False,
        model_name="distilbert",
        desired_error=0.01,
        max_training_time=7200,
        custom_tokenizer=None,
        sent_embedder="mean_norm",
        batch_size=10,
        max_position_embeddings=None,
        custom_train=True,
        frozen=False,
    ):
        super().__init__(is_target)

        self.name = model_name + " text encoder"

        # Token/sequence treatment
        self._pad_id = None
        self._max_len = max_position_embeddings
        self._custom_train = custom_train
        self._frozen = frozen
        self._batch_size = batch_size

        # Model details
        self.desired_error = desired_error
        self.max_training_time = max_training_time
        self._head = None

        # Model setup
        self._tokenizer = custom_tokenizer
        self._model = None
        self.model_type = None

        if model_name == "distilbert":
            self._classifier_model_class = DistilBertForSequenceClassification
            self._embeddings_model_class = DistilBertModel
            self._tokenizer_class = DistilBertTokenizerFast
            self._pretrained_model_name = "distilbert-base-uncased"

        elif model_name == "albert":
            self._classifier_model_class = AlbertForSequenceClassification
            self._embeddings_model_class = AlbertModel
            self._tokenizer_class = AlbertTokenizerFast
            self._pretrained_model_name = "albert-base-v2"

        elif model_name == "bart":
            self._classifier_model_class = BartForSequenceClassification
            self._embeddings_model_class = BartModel
            self._tokenizer_class = BartTokenizerFast
            self._pretrained_model_name = "facebook/bart-large"

        else:
            self._classifier_model_class = GPT2ForSequenceClassification
            self._embeddings_model_class = GPT2Model
            self._tokenizer_class = GPT2TokenizerFast
            self._pretrained_model_name = "gpt2"

        # Type of sentence embedding
        if sent_embedder == "last_token":
            self._sent_embedder = self._last_state
        else:
            self._sent_embedder = self._mean_norm

        self.device, _ = get_devices()

    def prepare(self, priming_data, training_data=None):
        """
        Prepare the encoder by training on the target.
        """
        if self._prepared:
            raise Exception("Encoder is already prepared.")

        # TODO: Make tokenizer custom with partial function; feed custom->model
        if self._tokenizer is None:
            # Set the default tokenizer
            self._tokenizer = self._tokenizer_class.from_pretrained(
                self._pretrained_model_name
            )

        # Replace empty strings with ''
        priming_data = [x if x is not None else "" for x in priming_data]

        # Check style of output

        # Case 1: Categorical, 1 output
        output_type = (
            training_data is not None
            and "targets" in training_data
            and len(training_data["targets"]) == 1
            and training_data["targets"][0]["output_type"]
            == COLUMN_DATA_TYPES.CATEGORICAL
        )

        if self._custom_train and output_type:

            # Prepare the priming data inputs with attention masks etc.
            text = self._tokenizer(priming_data, truncation=True, padding=True)

            xinp = TextEmbed(text, training_data["targets"][0]["unencoded_output"])

            # Pad the text tokens on the left
            dataset = DataLoader(xinp, batch_size=self._batch_size, shuffle=True)

            # Construct the model
            self._model = self._classifier_model_class.from_pretrained(
                self._pretrained_model_name,
                num_labels=len(set(training_data["targets"][0]["unencoded_output"]))
                + 1,
            ).to(self.device)

            # If max length not set, adjust
            if self._max_len is None:
                if "gpt2" in self._pretrained_model_name:
                    self._max_len = self._model.config.n_positions
                else:
                    self._max_len = self._model.config.max_position_embeddings

            if self._frozen:
                """
                Freeze the base transformer model and train
                a linear layer on top
                """
                #Freeze all the parameters
                for param in self._model.base_model.parameters():
                    param.requires_grad = False

                optimizer_grouped_parameters = self._model.parameters()

            else:
                """
                Fine-tuning parameters with weight decay
                """
                # Fine-tuning weight-decay (https://huggingface.co/transformers/training.html)
                no_decay = ['bias', 'LayerNorm.weight'] # no decay on the classifier terms.
                optimizer_grouped_parameters = [
                    {'params': [p for n, p in self._model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
                    {'params': [p for n, p in self._model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
                ]

            optimizer = AdamW(optimizer_grouped_parameters, lr=1e-5)

            # Train model; declare optimizer earlier if desired.
            self._train_model(dataset, optim=optimizer, n_epochs=2)

            self.prepared = True

        else:
            print("Embeddings Generator only")

            self.model_type = "embeddings_generator"
            self._model = self._embeddings_model_class.from_pretrained(
                self._pretrained_model_name
            ).to(self.device)

    def _train_model(self, dataset, optim=None, n_epochs=4):
        """
        Given a model, train for n_epochs.

        model - torch.nn model;
        dataset - torch.DataLoader; dataset to train
        device - torch.device; cuda/cpu
        log - lightwood.logger.log; print output
        optim - transformers.optimization.AdamW; optimizer
        n_epochs - number of epochs to train

        """
        self._model.train()

        if optim is None:
            print("Model Params")
            optim = AdamW(self._model.parameters(), lr=5e-5)

        for epoch in range(n_epochs):
            for batch in dataset:
                optim.zero_grad()

                inpids = batch["input_ids"].to(self.device)
                attn = batch["attention_mask"].to(self.device)
                labels = batch["labels"].to(self.device)
                outputs = self._model(inpids, attention_mask=attn, labels=labels)
                loss = outputs[0]

                loss.backward()
                optim.step()

            self._train_callback(epoch, loss.item())
            print("Epoch=", epoch + 1, "Loss=", loss.item())

    def _train_callback(self, epoch, loss):
        log.info(f"{self.name} at epoch {epoch+1} and loss {loss}!")

    def encode(self, column_data):
        """
        TODO: Maybe batch the text up; may take too long
        Given column data, encode the dataset
        Tokenizer should have a length cap!!

        Args:
        column_data:: [list[str]] list of text data in str form

        Returns:
        encoded_representation:: [torch.Tensor] N_sentences x Nembed_dim
        """
        encoded_representation = []

        # Freeze training mode while encoding
        self._model.eval()

        with torch.no_grad():
            # Set the weights; this is GPT-2
            if self._model_type == "embeddings_generator":
                for text in column_data:

                    # Omit NaNs
                    if text == None:
                        text = ""

                    # Tokenize the text with the built-in tokenizer.
                    inp = self._tokenizer.encode(
                        text, truncation=True, return_tensors="pt"
                    ).to(self.device)

                    # TODO - try different accumulation techniques?
                    # TODO: Current hack is to keep the first max len
                    # inp = inp[:, : self._max_len]

                    output = self._model(inp).last_hidden_state
                    output = self._sent_embedder(output.to(self.device))

                    encoded_representation.append(output)

        return torch.Tensor(encoded_representation).squeeze(1)

    def decode(self, encoded_values_tensor, max_length=100):
        raise Exception("Decoder not implemented yet.")

    @staticmethod
    def _mean_norm(xinp, dim=1):
        """
        Calculates a 1 x N_embed vector by averaging all token embeddings

        Args:
        xinp ::torch.Tensor; Assumes order Nbatch x Ntokens x Nembedding
        dim ::int; dimension to average on
        """
        return torch.mean(xinp, dim=dim).cpu().numpy()

    @staticmethod
    def _last_state(xinp):
        """
        Returns the last token in the sentence only

        Args:
            xinp ::torch.Tensor; Assumes order Nbatch x Ntokens x Nembedding
        """
        return xinp[:, -1, :].cpu().numpy()