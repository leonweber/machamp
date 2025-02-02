from typing import Dict, List, Tuple, Iterable, Any

import numpy
import torch
import torch.nn.functional as F
from allennlp.data import TextFieldTensors, Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Attention
from allennlp.modules.token_embedders import Embedding
from allennlp.nn import util
from allennlp.nn.beam_search import BeamSearch
from allennlp.training.metrics import BLEU
from overrides import overrides
from torch.nn.modules.linear import Linear
from torch.nn.modules.rnn import LSTMCell, LSTM


@Model.register("machamp_seq2seq_decoder")
class MachampSeq2SeqDecoder(Model):
    """
    An autoregressive decoder that can be used for most seq2seq tasks.

    # Parameters

    vocab : `Vocabulary`, required
        Vocabulary containing source and target vocabularies. They may be under the same namespace
        (`tokens`) or the target tokens can have a different namespace, in which case it needs to
        be specified as `target_namespace`.
    max_decoding_steps : `int`
        Maximum length of decoded sequences.
    attention : `Attention`, optional (default = `None`)
        If you want to use attention to get a dynamic summary of the encoder outputs at each step
        of decoding, this is the function used to compute similarity between the decoder hidden
        state and encoder outputs.
    target_namespace : `str`, optional (default = `'tokens'`)
        If the target side vocabulary is different from the source side's, you need to specify the
        target's namespace here. If not, we'll assume it is "tokens", which is also the default
        choice for the source side, and this might cause them to share vocabularies.
    target_embedding_dim : `int`, optional (default = `'source_embedding_dim'`)
        You can specify an embedding dimensionality for the target side. If not, we'll use the same
        value as the source embedder's.
    beam_size : `int`, optional (default = `None`)
        Width of the beam for beam search. If not specified, greedy decoding is used.
    scheduled_sampling_ratio : `float`, optional (default = `0.`)
        At each timestep during training, we sample a random number between 0 and 1, and if it is
        not less than this value, we use the ground truth labels for the whole batch. Else, we use
        the predictions from the previous time step for the whole batch. If this value is 0.0
        (default), this corresponds to teacher forcing, and if it is 1.0, it corresponds to not
        using target side ground truth labels.  See the following paper for more information:
        [Scheduled Sampling for Sequence Prediction with Recurrent Neural Networks. Bengio et al.,
        2015](https://arxiv.org/abs/1506.03099).
    use_bleu : `bool`, optional (default = `True`)
        If True, the BLEU metric will be calculated during validation.
    ngram_weights : `Iterable[float]`, optional (default = `(0.25, 0.25, 0.25, 0.25)`)
        Weights to assign to scores for each ngram size.
    """

    def __init__(
        self,
        task: str,
        vocab: Vocabulary,
        input_dim: int,
        max_decoding_steps: int,
        loss_weight: float = 1.0,
        attention: Attention = None,
        beam_size: int = None,
        target_embedding_dim: int = None,
        scheduled_sampling_ratio: float = 0.0,
        use_bleu: bool = True,
        bleu_ngram_weights: Iterable[float] = (0.25, 0.25, 0.25, 0.25),
        dataset_embeds_dim: int = 0,
        target_decoder_layers: int = 1,
        **kwargs,
    ) -> None:

        super().__init__(vocab, **kwargs)

        self.task = task
        self.vocab = vocab
        self.loss_weight = loss_weight
        self._target_namespace = 'target_words'
        self._target_decoder_layers = target_decoder_layers
        self._scheduled_sampling_ratio = scheduled_sampling_ratio

        # We need the start symbol to provide as the input at the first timestep of decoding, and
        # end symbol as a way to indicate the end of the decoded sequence.
        # TODO: don't use hardcoded indices
        self._start_index = 2   # self.vocab.get_token_index('[CLS]', self._target_namespace)
        self._end_index = 3     # self.vocab.get_token_index('[SEP]', self._target_namespace)

        pad_index = self.vocab.get_token_index(
            self.vocab._padding_token, self._target_namespace
        )

        if use_bleu:
            self._bleu = BLEU(
                bleu_ngram_weights, exclude_indices={pad_index, self._end_index, self._start_index}
            )
        else:
            self._bleu = None
        self.metrics = {"bleu": self._bleu}

        # At prediction time, we use a beam search to find the most likely sequence of target tokens.
        beam_size = beam_size or 1
        self._max_decoding_steps = max_decoding_steps
        self._beam_search = BeamSearch(
            self._end_index, max_steps=max_decoding_steps, beam_size=beam_size
        )

        num_classes = self.vocab.get_vocab_size(namespace=self._target_namespace)

        # Attention mechanism applied to the encoder output for each step.
        self._attention = attention

        # The input to the decoder is just the previous target embedding.
        target_embedding_dim = target_embedding_dim or self._encoder_output_dim
        self._decoder_input_dim = target_embedding_dim

        # Dense embedding of vocab words in the target space.
        self._target_embedder = Embedding(
            num_embeddings=num_classes, embedding_dim=target_embedding_dim, padding_index=pad_index
        )

        # Decoder output dim needs to be the same as the encoder output dim since we initialize the
        # hidden state of the decoder with the final hidden state of the encoder.
        self._encoder_output_dim = input_dim + dataset_embeds_dim
        self._decoder_output_dim = self._encoder_output_dim

        if self._attention:
            # If using attention, a weighted average over encoder outputs will be concatenated
            # to the previous target embedding to form the input to the decoder at each
            # time step.
            self._decoder_input_dim = self._decoder_output_dim + target_embedding_dim
        else:
            # Otherwise, the input to the decoder is just the previous target embedding.
            self._decoder_input_dim = target_embedding_dim

        # We'll use an LSTM cell as the recurrent cell that produces a hidden state
        # for the decoder at each time step.
        if self._target_decoder_layers > 1:
            self._decoder_cell = LSTM(
                self._decoder_input_dim, self._decoder_output_dim, self._target_decoder_layers,
            )
        else:
            self._decoder_cell = LSTMCell(self._decoder_input_dim, self._decoder_output_dim)
        # We project the hidden state from the decoder into the output vocabulary space
        # in order to get log probabilities of each target token, at each time step.
        self._output_projection_layer = Linear(self._decoder_output_dim, num_classes)

    @overrides
    def forward(
        self,  # type: ignore
        embedded_text: torch.LongTensor,
        source_mask: torch.LongTensor,
        target_tokens: TextFieldTensors = None
    ) -> Dict[str, torch.Tensor]:

        state = {"encoder_outputs": embedded_text, "source_mask": source_mask}
        if target_tokens:
            state = self._init_decoder_state(state)
            # The `_forward_loop` decodes the input sequence and computes the loss during training
            # and validation.
            output_dict = self._forward_loop(state, target_tokens)
        else:
            output_dict = {}

        if not self.training:
            state = self._init_decoder_state(state)
            predictions = self._forward_beam_search(state)
            output_dict.update(predictions)
            if target_tokens and self._bleu:
                # shape: (batch_size, beam_size, max_sequence_length)
                top_k_predictions = output_dict["predictions"]
                # shape: (batch_size, max_predicted_sequence_length)
                best_predictions = top_k_predictions[:, 0, :]
                self._bleu(best_predictions, target_tokens["tokens"]["tokens"])

        return output_dict

    @overrides
    def make_output_human_readable(self, output_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Finalize predictions.
        This method overrides `Model.make_output_human_readable`, which gets called after `Model.forward`, at test
        time, to finalize predictions. The logic for the decoder part of the encoder-decoder lives
        within the `forward` method.
        This method trims the output predictions to the first end symbol, replaces indices with
        corresponding tokens, and adds a field called `predicted_tokens` to the `output_dict`.
        """
        predicted_indices = output_dict#["predictions"]
        if not isinstance(predicted_indices, numpy.ndarray):
            predicted_indices = predicted_indices.detach().cpu().numpy()
        all_predicted_tokens = []
        for top_k_predictions in predicted_indices:
            # Beam search gives us the top k results for each source sentence in the batch
            # we want top-k results.
            if len(top_k_predictions.shape) == 1:
                top_k_predictions = [top_k_predictions]

            batch_predicted_tokens = []
            for indices in top_k_predictions:
                indices = list(indices)
                # Collect indices till the first end_symbol
                if self._end_index in indices:
                    indices = indices[: indices.index(self._end_index)]
                predicted_tokens = [
                    self.vocab.get_token_from_index(x, namespace=self._target_namespace)
                    for x in indices
                ]
                batch_predicted_tokens.append(predicted_tokens)

            all_predicted_tokens.append(batch_predicted_tokens)
        return all_predicted_tokens

    def take_step(
        self, last_predictions: torch.Tensor, state: Dict[str, torch.Tensor], step: int
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Take a decoding step. This is called by the beam search class.
        # Parameters
        last_predictions : `torch.Tensor`
            A tensor of shape `(group_size,)`, which gives the indices of the predictions
            during the last time step.
        state : `Dict[str, torch.Tensor]`
            A dictionary of tensors that contain the current state information
            needed to predict the next step, which includes the encoder outputs,
            the source mask, and the decoder hidden state and context. Each of these
            tensors has shape `(group_size, *)`, where `*` can be any other number
            of dimensions.
        step : `int`
            The time step in beam search decoding.
        # Returns
        Tuple[torch.Tensor, Dict[str, torch.Tensor]]
            A tuple of `(log_probabilities, updated_state)`, where `log_probabilities`
            is a tensor of shape `(group_size, num_classes)` containing the predicted
            log probability of each class for the next step, for each item in the group,
            while `updated_state` is a dictionary of tensors containing the encoder outputs,
            source mask, and updated decoder hidden state and context.
        Notes
        -----
            We treat the inputs as a batch, even though `group_size` is not necessarily
            equal to `batch_size`, since the group may contain multiple states
            for each source sentence in the batch.
        """
        # shape: (group_size, num_classes)
        output_projections, state = self._prepare_output_projections(last_predictions, state)

        # shape: (group_size, num_classes)
        class_log_probabilities = F.log_softmax(output_projections, dim=-1)

        return class_log_probabilities, state

    def _init_decoder_state(self, state: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        batch_size = state["source_mask"].size(0)
        # shape: (batch_size, encoder_output_dim)
        final_encoder_output = util.get_final_encoder_states(
            state["encoder_outputs"], state["source_mask"], bidirectional=False
        )
        # Initialize the decoder hidden state with the final output of the encoder.
        # shape: (batch_size, decoder_output_dim)
        state["decoder_hidden"] = final_encoder_output
        # shape: (batch_size, decoder_output_dim)
        state["decoder_context"] = state["encoder_outputs"].new_zeros(
            batch_size, self._decoder_output_dim
        )
        if self._target_decoder_layers > 1:
            # shape: (num_layers, batch_size, decoder_output_dim)
            state["decoder_hidden"] = (
                state["decoder_hidden"].unsqueeze(0).repeat(self._target_decoder_layers, 1, 1)
            )

            # shape: (num_layers, batch_size, decoder_output_dim)
            state["decoder_context"] = (
                state["decoder_context"].unsqueeze(0).repeat(self._target_decoder_layers, 1, 1)
            )

        return state

    def _forward_loop(
        self, state: Dict[str, torch.Tensor], target_tokens: TextFieldTensors = None
    ) -> Dict[str, torch.Tensor]:
        """
        Make forward pass during training or do greedy search during prediction.
        Notes
        -----
        We really only use the predictions from the method to test that beam search
        with a beam size of 1 gives the same results.
        """
        # shape: (batch_size, max_input_sequence_length)
        source_mask = state["source_mask"]

        batch_size = source_mask.size()[0]

        if target_tokens:
            # shape: (batch_size, max_target_sequence_length)
            targets = target_tokens["tokens"]["tokens"]

            _, target_sequence_length = targets.size()

            # The last input from the target is either padding or the end symbol.
            # Either way, we don't have to process it.
            num_decoding_steps = target_sequence_length - 1
        else:
            num_decoding_steps = self._max_decoding_steps

        # Initialize target predictions with the start index.
        # shape: (batch_size,)
        last_predictions = source_mask.new_full(
            (batch_size,), fill_value=self._start_index, dtype=torch.long
        )

        step_logits: List[torch.Tensor] = []
        step_predictions: List[torch.Tensor] = []

        for timestep in range(num_decoding_steps):
            if self.training and torch.rand(1).item() < self._scheduled_sampling_ratio:
                # Use gold tokens at test time and at a rate of 1 - _scheduled_sampling_ratio
                # during training.
                # shape: (batch_size,)
                input_choices = last_predictions
            elif not target_tokens:
                # shape: (batch_size,)
                input_choices = last_predictions
            else:
                # shape: (batch_size,)
                input_choices = targets[:, timestep]

            # shape: (batch_size, num_classes)
            output_projections, state = self._prepare_output_projections(input_choices, state)

            # list of tensors, shape: (batch_size, 1, num_classes)
            step_logits.append(output_projections.unsqueeze(1))

            # shape: (batch_size, num_classes)
            class_probabilities = F.softmax(output_projections, dim=-1)

            # shape (predicted_classes): (batch_size,)
            _, predicted_classes = torch.max(class_probabilities, 1)

            # shape (predicted_classes): (batch_size,)
            last_predictions = predicted_classes

            step_predictions.append(last_predictions.unsqueeze(1))

        # shape: (batch_size, num_decoding_steps)
        predictions = torch.cat(step_predictions, 1)

        output_dict = {"predictions": predictions, "class_probabilities": predictions}

        if target_tokens:
            # shape: (batch_size, num_decoding_steps, num_classes)
            logits = torch.cat(step_logits, 1)

            # Compute loss.
            target_mask = util.get_text_field_mask(target_tokens)
            loss = self._get_loss(logits, targets, target_mask) * self.loss_weight
            output_dict["loss"] = loss
            output_dict['loss'] /= torch.log(torch.tensor(logits.shape[-1]))

        return output_dict

    def _forward_beam_search(self, state: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """Make forward pass during prediction using a beam search."""
        batch_size = state["source_mask"].size()[0]
        start_predictions = state["source_mask"].new_full(
            (batch_size,), fill_value=self._start_index, dtype=torch.long
        )

        # shape (all_top_k_predictions): (batch_size, beam_size, num_decoding_steps)
        # shape (log_probabilities): (batch_size, beam_size)
        all_top_k_predictions, log_probabilities = self._beam_search.search(
            start_predictions, state, self.take_step
        )

        output_dict = {
            "class_log_probabilities": log_probabilities,
            "predictions": all_top_k_predictions,
        }
        return output_dict

    def _prepare_output_projections(
        self, last_predictions: torch.Tensor, state: Dict[str, torch.Tensor]
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Decode current state and last prediction to produce produce projections
        into the target space, which can then be used to get probabilities of
        each target token for the next step.
        Inputs are the same as for `take_step()`.
        """
        # shape: (group_size, max_input_sequence_length, encoder_output_dim)
        encoder_outputs = state["encoder_outputs"]

        # shape: (group_size, max_input_sequence_length)
        source_mask = state["source_mask"]

        # shape: (num_layers, group_size, decoder_output_dim)
        decoder_hidden = state["decoder_hidden"]

        # shape: (num_layers, group_size, decoder_output_dim)
        decoder_context = state["decoder_context"]

        # shape: (group_size, target_embedding_dim)
        embedded_input = self._target_embedder(last_predictions)

        if self._attention:
            # shape: (group_size, encoder_output_dim)
            if self._target_decoder_layers > 1:
                attended_input = self._prepare_attended_input(
                    decoder_hidden[0], encoder_outputs, source_mask
                )
            else:
                attended_input = self._prepare_attended_input(
                    decoder_hidden, encoder_outputs, source_mask
                )
            # shape: (group_size, decoder_output_dim + target_embedding_dim)
            decoder_input = torch.cat((attended_input, embedded_input), -1)
        else:
            # shape: (group_size, target_embedding_dim)
            decoder_input = embedded_input

        if self._target_decoder_layers > 1:
            # shape: (1, batch_size, target_embedding_dim)
            #TODO why is this necessary? 
            decoder_input = decoder_input.unsqueeze(0).contiguous()
            decoder_context = decoder_context.contiguous()
            decoder_hidden = decoder_hidden.contiguous()

            # shape (decoder_hidden): (num_layers, batch_size, decoder_output_dim)
            # shape (decoder_context): (num_layers, batch_size, decoder_output_dim)
            # TODO (epwalsh): remove the autocast(False) once torch's AMP is working for LSTMCells.
            with torch.cuda.amp.autocast(False):
                _, (decoder_hidden, decoder_context) = self._decoder_cell(
                    decoder_input.float(), (decoder_hidden.float(), decoder_context.float())
                )
        else:
            # shape (decoder_hidden): (batch_size, decoder_output_dim)
            # shape (decoder_context): (batch_size, decoder_output_dim)
            # TODO (epwalsh): remove the autocast(False) once torch's AMP is working for LSTMCells.
            with torch.cuda.amp.autocast(False):
                decoder_hidden, decoder_context = self._decoder_cell(
                    decoder_input.float(), (decoder_hidden.float(), decoder_context.float())
                )

        state["decoder_hidden"] = decoder_hidden
        state["decoder_context"] = decoder_context

        # shape: (group_size, num_classes)
        if self._target_decoder_layers > 1:
            output_projections = self._output_projection_layer(decoder_hidden[-1])
        else:
            output_projections = self._output_projection_layer(decoder_hidden)
        return output_projections, state

    def _prepare_attended_input(
        self,
        decoder_hidden_state: torch.LongTensor = None,
        encoder_outputs: torch.LongTensor = None,
        encoder_outputs_mask: torch.BoolTensor = None,
    ) -> torch.Tensor:
        """Apply attention over encoder outputs and decoder state."""
        # shape: (batch_size, max_input_sequence_length)
        input_weights = self._attention(decoder_hidden_state, encoder_outputs, encoder_outputs_mask)

        # shape: (batch_size, encoder_output_dim)
        attended_input = util.weighted_sum(encoder_outputs, input_weights)

        return attended_input

    @staticmethod
    def _get_loss(
        logits: torch.LongTensor, targets: torch.LongTensor, target_mask: torch.BoolTensor,
    ) -> torch.Tensor:
        """
        Compute loss.
        Takes logits (unnormalized outputs from the decoder) of size (batch_size,
        num_decoding_steps, num_classes), target indices of size (batch_size, num_decoding_steps+1)
        and corresponding masks of size (batch_size, num_decoding_steps+1) steps and computes cross
        entropy loss while taking the mask into account.
        The length of `targets` is expected to be greater than that of `logits` because the
        decoder does not need to compute the output corresponding to the last timestep of
        `targets`. This method aligns the inputs appropriately to compute the loss.
        During training, we want the logit corresponding to timestep i to be similar to the target
        token from timestep i + 1. That is, the targets should be shifted by one timestep for
        appropriate comparison.  Consider a single example where the target has 3 words, and
        padding is to 7 tokens.
           The complete sequence would correspond to <S> w1  w2  w3  <E> <P> <P>
           and the mask would be                     1   1   1   1   1   0   0
           and let the logits be                     l1  l2  l3  l4  l5  l6
        We actually need to compare:
           the sequence           w1  w2  w3  <E> <P> <P>
           with masks             1   1   1   1   0   0
           against                l1  l2  l3  l4  l5  l6
           (where the input was)  <S> w1  w2  w3  <E> <P>
        """
        # shape: (batch_size, num_decoding_steps)
        relevant_targets = targets[:, 1:].contiguous()

        # shape: (batch_size, num_decoding_steps)
        relevant_mask = target_mask[:, 1:].contiguous()

        return util.sequence_cross_entropy_with_logits(logits, relevant_targets, relevant_mask)

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        main_metrics: Dict[str, float] = {}
        if self._bleu:# and not self.training:
            main_metrics = {
                f".run/{self.task}/{metric_name}": metric.get_metric(reset)
                for metric_name, metric in self.metrics.items()
            }
        return {**main_metrics}
