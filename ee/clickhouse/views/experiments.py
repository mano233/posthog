from typing import Any

from rest_framework import request, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from ee.clickhouse.queries.experiments.funnel_experiment_result import ClickhouseFunnelExperimentResult
from ee.clickhouse.queries.experiments.trend_experiment_result import ClickhouseTrendExperimentResult
from posthog.api.feature_flag import FeatureFlagSerializer
from posthog.api.routing import StructuredViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.constants import INSIGHT_TRENDS
from posthog.models.experiment import Experiment
from posthog.models.feature_flag import FeatureFlag
from posthog.models.filters.filter import Filter
from posthog.models.team import Team
from posthog.permissions import ProjectMembershipNecessaryPermissions, TeamMemberAccessPermission


class ExperimentSerializer(serializers.ModelSerializer):

    feature_flag_key = serializers.CharField(source="get_feature_flag_key")
    created_by = UserBasicSerializer(read_only=True)

    class Meta:
        model = Experiment
        fields = [
            "id",
            "name",
            "description",
            "start_date",
            "end_date",
            "feature_flag_key",
            "parameters",
            "filters",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def validate_parameters(self, value):
        if not value:
            return value

        variants = value.get("feature_flag_variants", [])

        if len(variants) > 4:
            raise ValidationError("Feature flag variants must be less than 5")
        elif len(variants) > 0:
            if "control" not in [variant["key"] for variant in variants]:
                raise ValidationError("Feature flag variants must contain a control variant")

        return value

    def create(self, validated_data: dict, *args: Any, **kwargs: Any) -> Experiment:

        if not validated_data.get("filters"):
            raise ValidationError("Filters are required to create an Experiment")

        variants = []
        if validated_data["parameters"]:
            variants = validated_data["parameters"].get("feature_flag_variants", [])

        request = self.context["request"]
        validated_data["created_by"] = request.user
        team = Team.objects.get(id=self.context["team_id"])

        feature_flag_key = validated_data.pop("get_feature_flag_key")

        is_draft = "start_date" not in validated_data or validated_data["start_date"] is None

        properties = validated_data["filters"].get("properties", [])

        default_variants = [
            {"key": "control", "name": "Control Group", "rollout_percentage": 50},
            {"key": "test", "name": "Test Variant", "rollout_percentage": 50},
        ]

        filters = {
            "groups": [{"properties": properties, "rollout_percentage": None}],
            "multivariate": {"variants": variants or default_variants},
        }

        feature_flag_serializer = FeatureFlagSerializer(
            data={
                "key": feature_flag_key,
                "name": f'Feature Flag for Experiment {validated_data["name"]}',
                "filters": filters,
                "active": not is_draft,
            },
            context=self.context,
        )

        feature_flag_serializer.is_valid(raise_exception=True)
        feature_flag = feature_flag_serializer.save()

        experiment = Experiment.objects.create(team=team, feature_flag=feature_flag, **validated_data)
        return experiment

    def update(self, instance: Experiment, validated_data: dict, *args: Any, **kwargs: Any) -> Experiment:
        has_start_date = "start_date" in validated_data
        feature_flag = instance.feature_flag

        expected_keys = set(["name", "description", "start_date", "end_date", "filters", "parameters"])
        given_keys = set(validated_data.keys())
        extra_keys = given_keys - expected_keys

        if feature_flag.key == validated_data.get("get_feature_flag_key"):
            extra_keys.remove("get_feature_flag_key")

        if extra_keys:
            raise ValidationError(f"Can't update keys: {', '.join(sorted(extra_keys))} on Experiment")

        if "feature_flag_variants" in validated_data.get("parameters", {}):

            if len(validated_data["parameters"]["feature_flag_variants"]) != len(feature_flag.variants):
                raise ValidationError("Can't update feature_flag_variants on Experiment")

            for variant in validated_data["parameters"]["feature_flag_variants"]:
                if (
                    len(
                        [
                            ff_variant
                            for ff_variant in feature_flag.variants
                            if ff_variant["key"] == variant["key"]
                            and ff_variant["rollout_percentage"] == variant["rollout_percentage"]
                        ]
                    )
                    != 1
                ):
                    raise ValidationError("Can't update feature_flag_variants on Experiment")

        if instance.is_draft and has_start_date:
            feature_flag.active = True
            feature_flag.save()
            return super().update(instance, validated_data)

        elif has_start_date:
            raise ValidationError("Can't change experiment start date after experiment has begun")
        else:
            # Not a draft, doesn't have start date
            # Or draft without start date
            return super().update(instance, validated_data)


class ClickhouseExperimentsViewSet(StructuredViewSetMixin, viewsets.ModelViewSet):
    serializer_class = ExperimentSerializer
    queryset = Experiment.objects.all()
    permission_classes = [IsAuthenticated, ProjectMembershipNecessaryPermissions, TeamMemberAccessPermission]

    def get_queryset(self):
        return super().get_queryset()

    # ******************************************
    # /projects/:id/experiments/:experiment_id/results
    #
    # Returns current results of an experiment, and graphs
    # 1. Probability of success
    # 2. Funnel breakdown graph to display
    # ******************************************
    @action(methods=["GET"], detail=True)
    def results(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        experiment: Experiment = self.get_object()

        if not experiment.filters:
            raise ValidationError("Experiment has no target metric")

        filter = Filter(experiment.filters)
        experiment_class = (
            ClickhouseTrendExperimentResult if filter.insight == INSIGHT_TRENDS else ClickhouseFunnelExperimentResult
        )

        result = experiment_class(
            filter, self.team, experiment.feature_flag, experiment.start_date, experiment.end_date,
        ).get_results()  # type: ignore # TODO: Fix type once I introduce base class

        return Response(result)
