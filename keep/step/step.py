import asyncio
import inspect
import logging
from dataclasses import field
from enum import Enum

import chevron
from pydantic.dataclasses import dataclass

from keep.conditions.condition_factory import ConditionFactory
from keep.contextmanager.contextmanager import ContextManager
from keep.exceptions.action_error import ActionError
from keep.iohandler.iohandler import IOHandler
from keep.providers.base.base_provider import BaseProvider
from keep.throttles.throttle_factory import ThrottleFactory


class StepType(Enum):
    STEP = "step"
    ACTION = "action"


class Step:
    def __init__(
        self,
        context_manager,
        step_id: str,
        config: dict,
        step_type: StepType,
        provider: BaseProvider,
        provider_parameters: dict,
    ):
        self.config = config
        self.step_id = step_id
        self.step_type = step_type
        self.provider = provider
        self.provider_parameters = provider_parameters
        self.context_manager = context_manager
        self.io_handler = IOHandler(context_manager)
        self.conditions = self.config.get("condition", [])
        self.conditions_results = {}
        self.logger = context_manager.get_logger()

    @property
    def foreach(self):
        return self.config.get("foreach")

    @property
    def name(self):
        return self.step_id

    def run(self):
        try:
            if self.config.get("foreach"):
                did_action_run = self._run_foreach()
            else:
                did_action_run = self._run_single()
            return did_action_run
        except Exception as e:
            raise ActionError(e)

    def _check_throttling(self, action_name):
        throttling = self.config.get("throttle")
        # if there is no throttling, return
        if not throttling:
            return False

        throttling_type = throttling.get("type")
        throttling_config = throttling.get("with")
        throttle = ThrottleFactory.get_instance(throttling_type, throttling_config)
        alert_id = self.context_manager.get_workflow_id()
        return throttle.check_throttling(action_name, alert_id)

    def _run_foreach(self):
        """Evaluate the action for each item, when using the `foreach` attribute (see foreach.md)"""
        # the item holds the value we are going to iterate over
        items = self.io_handler.render(self.config.get("foreach"))
        any_action_run = False
        # apply ALL conditions (the decision whether to run or not is made in the end)
        for item in items:
            self.context_manager.set_for_each_context(item)
            did_action_run = self._run_single()
            # If at least one item triggered an action, return True
            # TODO - do it per item
            if did_action_run:
                any_action_run = True
        return any_action_run

    def _run_single(self):
        # Initialize all conditions
        conditions = []

        for condition in self.conditions:
            condition_name = condition.get("name", None)

            if not condition_name:
                raise Exception("Condition must have a name")

            conditions.append(
                ConditionFactory.get_condition(
                    self.context_manager,
                    condition.get("type"),
                    condition_name,
                    condition,
                )
            )

        for condition in conditions:
            condition_compare_to = condition.get_compare_to()
            condition_compare_value = condition.get_compare_value()
            condition_result = condition.apply(
                condition_compare_to, condition_compare_value
            )
            self.context_manager.set_condition_results(
                self.step_id,
                condition.condition_name,
                condition.condition_type,
                condition_compare_to,
                condition_compare_value,
                condition_result,
                condition_alias=condition.condition_alias,
                **condition.condition_context,
            )

        # Second, decide if need to run
        # after all conditions are applied, check if we need to run
        # there are 2 cases:
        # 1. a "if" block is supplied, then use it
        # 2. no "if" block is supplied, then use the AND between all conditions
        if self.config.get("if"):
            if_conf = self.config.get("if")
        else:
            # create a string of all conditions, separated by "and"
            if_conf = " and ".join(
                [f"{{{{ {condition.condition_alias} }}}} " for condition in conditions]
            )

        # Now check it
        if if_conf:
            if_met = self.io_handler.render(if_conf)
            # Evaluate the condition string
            if_met = eval(if_met)
        else:
            if_met = True

        if not if_met:
            self.logger.info(
                "Action %s evaluated NOT to run, Reason: %s evaluated to false.",
                self.config.get("name"),
                if_conf,
            )
            return

        self.logger.info("Action %s evaluated to run!", self.config.get("name"))

        # Third, check throttling
        # Now check if throttling is enabled
        throttled = self._check_throttling(self.config.get("name"))
        if throttled:
            self.logger.info("Action %s is throttled", self.config.get("name"))
            return

        # Last, run the action
        rendered_value = self.io_handler.render_context(self.provider_parameters)
        # if the provider is async, run it in a new event loop
        if inspect.iscoroutinefunction(self.provider.notify):
            result = self._run_single_async()
        # else, just run the provider
        else:
            try:
                rendered_providers_parameters = {}
                for parameter in self.provider_parameters:
                    rendered_providers_parameters[parameter] = self.io_handler.render(
                        self.provider_parameters[parameter]
                    )

                if self.step_type == StepType.STEP:
                    step_output = self.provider.query(**rendered_value)
                    self.context_manager.set_step_context(
                        self.step_id, results=step_output, foreach=self.foreach
                    )
                else:
                    self.provider.notify(**rendered_value)

                extra_context = self.provider.expose()
                rendered_providers_parameters.update(extra_context)
                self.context_manager.set_step_provider_paremeters(
                    self.step_id, rendered_providers_parameters
                )
            except Exception as e:
                raise StepError(e)

            return True

    def _run_single_async(self):
        """For async providers, run them in a new event loop

        Raises:
            ActionError: _description_
        """
        rendered_value = self.io_handler.render_context(self.provider_parameters)
        # This is "magically solved" because of nest_asyncio but probably isn't best practice
        loop = asyncio.new_event_loop()
        if self.step_type == StepType.STEP:
            task = loop.create_task(self.provider.query(**rendered_value))
        else:
            task = loop.create_task(self.provider.notify(**rendered_value))
        try:
            loop.run_until_complete(task)
        except Exception as e:
            raise ActionError(e)


class StepError(Exception):
    pass
