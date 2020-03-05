#!/usr/bin/env python3
import warnings
import torch
import torch.nn as nn
import copy

from ..._utils.attribution import LayerAttribution
from ..._utils.common import _format_attributions, _format_input, _run_forward
from ..._utils.gradient import (
    apply_gradient_requirements,
    undo_gradient_requirements,
    compute_gradients,
)
from ..._core.layer_wise_relevance_propagation import LRP

class LayerLRP(LRP, LayerAttribution):
    def __init__(self, model, layer):
        """
        Args:

            model (callable): The forward function of the model or
                        any modification of it. Custom rules for a given layer need to be defined as attribute
                        `module.rule` and need to be of type PropagationRule.
            rules (dictionary(int, PropagationRule)): Dictionary of layer index and Rules for specific layers
                        of forward_func.
        """
        LayerAttribution.__init__(self, model, layer)
        LRP.__init__(self, model)


    def attribute(
        self,
        inputs,
        target=None,
        additional_forward_args=None,
        return_convergence_delta=False,
        return_for_all_layers=False,
        verbose=False,
    ):
        """
            Layer-wise relevance propagation is based on a backward propagation mechanism applied sequentially
            to all layers of the model. Here, the model output score represents the initial relevance which is
            decomposed into values for each neuron of the underlying layers. The decomposition is defined
            by rules that are chosen for each layer, involving its weights and activations. Details on the model
            can be found in the original paper [https://doi.org/10.1371/journal.pone.0130140] and on the implementation
            and rules in the tutorial paper [https://doi.org/10.1016/j.dsp.2017.10.011].

            Attention: The implementation is only tested for ReLU activation layers, linear and conv2D layers. An error
            is raised if other activation functions (sigmoid, tanh) are used.

            Args:
                inputs (tensor or tuple of tensors):  Input for which relevance is propagated.
                            If forward_func takes a single
                            tensor as input, a single input tensor should be provided.
                            If forward_func takes multiple tensors as input, a tuple
                            of the input tensors should be provided. It is assumed
                            that for all given input tensors, dimension 0 corresponds
                            to the number of examples, and if multiple input tensors
                            are provided, the examples must be aligned appropriately.
                target (int, tuple, tensor or list, optional):  Output indices for
                            which gradients are computed (for classification cases,
                            this is usually the target class).
                            If the network returns a scalar value per example,
                            no target index is necessary.
                            For general 2D outputs, targets can be either:

                        - a single integer or a tensor containing a single
                            integer, which is applied to all input examples

                        - a list of integers or a 1D tensor, with length matching
                            the number of examples in inputs (dim 0). Each integer
                            is applied as the target for the corresponding example.

                        For outputs with > 2 dimensions, targets can be either:

                        - A single tuple, which contains #output_dims - 1
                            elements. This target index is applied to all examples.

                        - A list of tuples with length equal to the number of
                            examples in inputs (dim 0), and each tuple containing
                            #output_dims - 1 elements. Each tuple is applied as the
                            target for the corresponding example.

                        Default: None
                additional_forward_args (tuple, optional): If the forward function
                        requires additional arguments other than the inputs for
                        which attributions should not be computed, this argument
                        can be provided. It must be either a single additional
                        argument of a Tensor or arbitrary (non-tuple) type or a tuple
                        containing multiple additional arguments including tensors
                        or any arbitrary python types. These arguments are provided to
                        forward_func in order, following the arguments in inputs.
                        Note that attributions are not computed with respect
                        to these arguments.
                        Default: None

                return_convergence_delta (bool, optional): Indicates whether to return
                        convergence delta or not. If `return_convergence_delta`
                        is set to True convergence delta will be returned in
                        a tuple following attributions.
                        Default: False

                return_for_all_layers (bool, optional): Indicates whether to return
                        relevance values for all layers. If False, only the relevance
                        values for the input layer are returned.

                verbose (bool, optional): Indicates whether information on application
                        of rules is printed during propagation.

        Returns:
            *tensor* or tuple of *tensors* of **attributions** or 2-element tuple of **attributions**, **delta**::
            - **attributions** (*tensor* or tuple of *tensors*):
                        The propagated relevance values with respect to each
                        input feature. Attributions will always
                        be the same size as the provided inputs, with each value
                        providing the attribution of the corresponding input index.
                        If a single tensor is provided as inputs, a single tensor is
                        returned. If a tuple is provided for inputs, a tuple of
                        corresponding sized tensors is returned. The sum of attributions
                        is one and not corresponding to the prediction score as in other
                        implementations.
            - **delta** (*tensor*, returned if return_convergence_delta=True):

                        Delta is calculated per example, meaning that the number of
                        elements in returned delta tensor is equal to the number of
                        of examples in input.
        Examples::

                >>> # ImageClassifier takes a single input tensor of images Nx3x32x32,
                >>> # and returns an Nx10 tensor of class probabilities. It has one
                >>> # Conv2D and a ReLU layer.
                >>> net = ImageClassifier()
                >>> lrp = LRP(net)
                >>> input = torch.randn(3, 3, 32, 32)
                >>> # Attribution size matches input size: 3x3x32x32
                >>> attribution = lrp.attribute(input, target=5)

        """
        self.verbose = verbose
        self.model = copy.deepcopy(self.original_model)
        self.layers = []
        self._get_layers(self.model)
        self._get_rules()
        self._check_if_weights_are_changed()
        self.return_for_all_layers = return_for_all_layers
        self.backward_handles = []
        self.forward_handles = []

        is_inputs_tuple = isinstance(inputs, tuple)
        inputs = _format_input(inputs)
        gradient_mask = apply_gradient_requirements(inputs)
        # 1. Forward pass
        self._change_weights(inputs)
        self._register_forward_hooks()
        # 2. Forward pass + backward pass
        relevances = compute_gradients(
            self.model, inputs, target, additional_forward_args
        )

        relevances = tuple(
            relevance * input for relevance, input in zip(relevances, inputs)
        )

        self._remove_backward_hooks()
        self._remove_forward_hooks()
        undo_gradient_requirements(inputs, gradient_mask)
        self._remove_rules()

        relevances = self._select_layer_output(self.layer)

        if return_convergence_delta:
            delta = self.compute_convergence_delta(
                relevances[0], inputs, additional_forward_args, target
            )
            return _format_attributions(is_inputs_tuple, relevances), delta
        else:
            return _format_attributions(is_inputs_tuple, relevances)


    def _select_layer_output(self, layer):
        return (layer.relevance)