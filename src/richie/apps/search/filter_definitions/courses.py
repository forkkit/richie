"""Define FilterDefinition classes for the courses index."""
from functools import reduce

from django import forms
from django.conf import settings
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from ..fields.array import ArrayField
from ..indexers import ES_INDICES
from ..utils.i18n import get_best_field_language
from .base import BaseChoicesFilterDefinition, BaseFilterDefinition
from .mixins import (
    ChoicesAggsMixin,
    ChoicesQueryMixin,
    NestedChoicesAggsMixin,
    TermsAggsMixin,
    TermsQueryMixin,
)


class IndexableFilterDefinition(TermsAggsMixin, TermsQueryMixin, BaseFilterDefinition):
    """
    Filter definition for a terms-based filter. The choices are generated dynamically from
    the incoming facets to avoid having to hold in memory or iterate over an unbounded
    number of choices.
    """

    def get_form_fields(self):
        """Indexables are filtered via a list of their Elasticsearch ids i.e strings."""
        return {
            self.name: ArrayField(
                required=False, base_type=forms.CharField(max_length=50)
            )
        }

    def get_i18n_names(self, keys):
        """
        Helper method to get the corresponding internationalized human name for each key in
        a list of indexed objects' ids.
        This covers the base case for terms e.g. other models in their own ElasticSearch index
        like organizations or categories.
        """
        language = translation.get_language()
        indexer = getattr(ES_INDICES, self.name)

        # Get just the documents we need from ElasticSearch
        search_query_response = settings.ES_CLIENT.search(
            # We only need the titles to get the i18n names
            _source=["title.*"],
            index=indexer.index_name,
            doc_type=indexer.document_type,
            body={
                "query": {
                    "terms": {
                        "_uid": [
                            "{:s}#{:s}".format(indexer.document_type, key)
                            for key in keys
                        ]
                    }
                }
            },
            size=len(keys),
        )

        # Extract the best available language here to avoid handling these kinds of
        # implementation details in the ViewSet
        return {
            doc["_id"]: get_best_field_language(doc["_source"]["title"], language)
            for doc in search_query_response["hits"]["hits"]
        }

    def get_faceted_definitions(self, facets, *args, **kwargs):
        """
        Build the filter definition's values from base definition and the faceted keys in the
        current language.
        Those provide us with the keys and counts that we just have to consume.
        We resort to the `get_i18n_names` method to get the internationalized human names.
        """
        # Convert the keys & counts from ElasticSearch facets to a more readily consumable format
        #   {
        #       self.name: {
        #           self.name: {
        #               "buckets": [
        #                   {"key": "A", "count": x}
        #                   {"key": "B", "count": y}
        #                   {"key": "C", "count": z}
        #               ]
        #           }
        #       }
        #   }
        # 👆 becomes 👇
        #   {"A": x, "B": y, "C": z}
        key_count_map = reduce(
            lambda agg, key_count_dict: {
                **agg,
                key_count_dict["key"]: key_count_dict["doc_count"],
            },
            facets[self.name][self.name]["buckets"],
            {},
        )

        # Get internationalized names for all our keys
        key_i18n_name_map = self.get_i18n_names([key for key in key_count_map])

        return {
            self.name: {
                # We always need to pass the base definition to the frontend
                "human_name": self.human_name,
                "is_drilldown": self.is_drilldown,
                "name": self.name,
                "position": self.position,
                "values": [
                    # Aggregate the information from right above to build the values
                    {"count": count, "human_name": key_i18n_name_map[key], "name": key}
                    for key, count in key_count_map.items()
                ],
            }
        }


class StaticChoicesFilterDefinition(
    ChoicesAggsMixin, ChoicesQueryMixin, BaseChoicesFilterDefinition
):
    """
    A filter definition for static choices ie that can be defined from the project settings.
    """

    def __init__(self, values, fragment_map, *args, **kwargs):
        """Record values and fragment map as attributes."""
        super().__init__(*args, **kwargs)
        self.values = values
        self.fragment_map = fragment_map

    def get_values(self):
        """Return the values recorded as attribute."""
        return self.values

    def get_fragment_map(self):
        """Return the fragment map recorded as attribute."""
        return self.fragment_map


class AvailabilityFilterDefinition(
    NestedChoicesAggsMixin, ChoicesQueryMixin, BaseChoicesFilterDefinition
):
    """
    Filter definition to allow filtering by availability. Hardcoded choices provided along with
    their Elasticsearch filtering fragment.
    """

    COMING_SOON, OPEN, ONGOING, ARCHIVED = "coming_soon", "open", "ongoing", "archived"

    def get_values(self):
        """Return the hardcoded values with internationalized human names."""
        return {
            self.OPEN: _("Open for enrollment"),
            self.COMING_SOON: _("Coming soon"),
            self.ONGOING: _("On-going"),
            self.ARCHIVED: _("Archived"),
        }

    def get_fragment_map(self):
        """Return the hardcoded query fragments updated to the current datetime."""
        return {
            self.OPEN: [
                {"range": {"course_runs.enrollment_start": {"lte": timezone.now()}}},
                {"range": {"course_runs.enrollment_end": {"gte": timezone.now()}}},
            ],
            self.COMING_SOON: [
                {"range": {"course_runs.start": {"gte": timezone.now()}}}
            ],
            self.ONGOING: [
                {"range": {"course_runs.start": {"lte": timezone.now()}}},
                {"range": {"course_runs.end": {"gte": timezone.now()}}},
            ],
            self.ARCHIVED: [{"range": {"course_runs.end": {"lte": timezone.now()}}}],
        }


class LanguagesFilterDefinition(
    NestedChoicesAggsMixin, TermsQueryMixin, BaseChoicesFilterDefinition
):
    """
    Languages need their own FilterDefinition subclass as there's a different way to get their
    human names from other terms-based filters and the filter is applied on the `course_runs`
    nested field.
    """

    def get_values(self):
        """Return the language values defined in the project's settings."""
        return settings.ALL_LANGUAGES_DICT

    def get_fragment_map(self):
        """Compute query fragments for each language defined in the project's settings."""
        return {
            language: [{"term": {"course_runs.languages": language}}]
            for language in settings.ALL_LANGUAGES_DICT
        }