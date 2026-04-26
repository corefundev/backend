"""
Subscription plan logic: specs, quota tracking, API enforcement.

Public entry points:
    from src.plans.plans import Plan, PLAN_SPECS, get_plan_spec
    from src.plans.quota import QuotaState, check_training_quota, record_training_started
    from src.plans.enforcement import (
        assert_can_train,
        clip_horizon_to_plan,
        assert_config_keys_allowed,
        model_display_name,
    )
"""
