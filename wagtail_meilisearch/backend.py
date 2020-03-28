# 3rd party
from operator import itemgetter
from functools import lru_cache
import meilisearch
from django.apps import apps
from django.db.models import Manager, Model, QuerySet, Case, Q, When
from wagtail.search.index import (
    FilterField, SearchField, RelatedFields, AutocompleteField, class_is_indexed,
    get_indexed_models
)
from django.utils.encoding import force_text
from wagtail.search.backends.base import (
    BaseSearchQueryCompiler, BaseSearchResults, BaseSearchBackend, EmptySearchResults
)


AUTOCOMPLETE_SUFFIX = '_ngrams'
FILTER_SUFFIX = '_filter'


def _get_field_mapping(field):
    if isinstance(field, FilterField):
        return field.field_name + FILTER_SUFFIX
    elif isinstance(field, AutocompleteField):
        return field.field_name + AUTOCOMPLETE_SUFFIX
    return field.field_name


class MeiliSearchModelIndex:

    """Creats a working index for each model sent to it.
    """

    def __init__(self, backend, model):
        """Initialise an index for `model`

        Args:
            backend (MeiliSearchBackend): A backend instance
            model (django.db.Model): Should be able to pass any model here but it's most
                likely to be a subclass of wagtail.core.models.Page
        """
        self.backend = backend
        self.client = backend.client
        self.model = model
        self.name = model._meta.label
        self.index = self._set_index(model)
        self.search_params = {
            'limit': 999999,
            'matches': 'true'
        }

    def _set_index(self, model):
        label = self._get_label(model)
        # if index doesn't exist, create
        try:
            self.client.get_index(label).get_settings()
        except Exception:
            index = self.client.create_index(uid=label, primary_key='id')
        else:
            index = self.client.get_index(label)

        return index

    def _get_label(self, model):
        label = model._meta.label.replace('.', '-')
        return label

    def _rebuild(self):
        self.index.delete()
        self._set_index(self.model)

    def add_model(self, model):
        # Adding done on initialisation
        pass

    def get_index_for_model(self, model):
        self._set_index(model)
        return self

    def prepare_value(self, value):
        if not value:
            return ''
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ', '.join(self.prepare_value(item) for item in value)
        if isinstance(value, dict):
            return ', '.join(self.prepare_value(item)
                             for item in value.values())
        if callable(value):
            return force_text(value())
        return force_text(value)

    def _get_document_fields(self, model, item):
        for field in model.get_search_fields():
            if isinstance(field, (SearchField, FilterField, AutocompleteField)):
                yield _get_field_mapping(field), self.prepare_value(field.get_value(item))
            if isinstance(field, RelatedFields):
                value = field.get_value(item)
                if isinstance(value, (Manager, QuerySet)):
                    qs = value.all()
                    for sub_field in field.fields:
                        sub_values = qs.values_list(sub_field.field_name, flat=True)
                        yield '{0}__{1}'.format(field.field_name, _get_field_mapping(sub_field)), \
                            self.prepare_value(list(sub_values))
                if isinstance(value, Model):
                    for sub_field in field.fields:
                        yield '{0}__{1}'.format(field.field_name, _get_field_mapping(sub_field)),\
                            self.prepare_value(sub_field.get_value(value))

    def prepare_body(self, obj):
        return [(value, boost) for field in self.search_fields
                for value, boost in self.prepare_field(obj, field)]

    def _create_document(self, model, item):
        doc_fields = dict(self._get_document_fields(model, item))
        doc_fields.update(id=item.id)
        document = {}
        document.update(doc_fields)
        return document

    def refresh(self):
        # TODO: Work out what this method is supposed to do because nothing is documented properly
        # It might want something to do with `client.get_indexes()`, but who knows, there's no
        # docstrings anywhere in the reference classes.
        pass

    def add_item(self, item):
        doc = self._create_document(self.model, item)
        rv = self.index.add_documents([doc])

    def add_items(self, item_model, items):
        prepared = []
        for item in items:
            doc = self._create_document(self.model, item)
            prepared.append(doc)

        rv = self.index.add_documents(prepared)
        return rv

    def delete_item(self, obj):
        self.index.delete_document(obj.id)

    def search(self, query):
        return self.index.search(query, self.search_params)

    def __str__(self):
        return self.name


class MeiliSearchRebuilder:
    def __init__(self, model_index):
        self.index = model_index
        self.uid = self.index._get_label(self.index.model)

    def start(self):
        # for now, we're going to just delete the passed index and re-create.
        # We may look at a better way in the future.
        model = self.index.model
        old_index = self.index.backend.client.get_index(self.uid)
        old_index.delete()
        new_index = self.index.backend.get_index_for_model(model)
        return new_index

    def finish(self):
        pass


class MeiliSearchQueryCompiler(BaseSearchQueryCompiler):

    def _process_filter(self, field_attname, lookup, value, check_only=False):
        import ipdb; ipdb.set_trace()
        return super()._process_filter(field_attname, lookup, value, check_only)


@lru_cache()
def get_descendant_models(model):
    """
    Borrowed from Wagtail-Whoosh
    Returns all descendants of a model
    e.g. for a search on Page, return [HomePage, ContentPage, Page] etc.
    """
    descendant_models = [
        other_model for other_model in apps.get_models() if issubclass(other_model, model)
    ]
    return descendant_models


class MeiliSearchResults(BaseSearchResults):
    supports_facet = False

    def _do_search(self):
        results = []

        qc = self.query_compiler
        model = qc.queryset.model
        models = get_descendant_models(model)
        terms = qc.query.query_string

        for m in models:
            index = self.backend.get_index_for_model(m)
            rv = index.search(terms)
            for item in rv['hits']:
                if item not in results:
                    results.append(item)

        """At this point we have a list of results that each look something like this...

        {
            'id': 45014,
            '_matchesInfo': {
                'title_filter': [
                    {'start': 0, 'length': 6}
                ],
                'title': [
                    {'start': 0, 'length': 6}
                ],
                'excerpt': [
                    {'start': 20, 'length': 6}
                ],
                'title_ngrams': [
                    {'start': 0, 'length': 6}
                ],
                'body': [
                    {'start': 66, 'length': 6},
                    {'start': 846, 'length': 6},
                    {'start': 1888, 'length': 6},
                    {'start': 2250, 'length': 6},
                    {'start': 2262, 'length': 6},
                    {'start': 2678, 'length': 6},
                    {'start': 3307, 'length': 6}
                ]
            }
        }
        """
        # Let's annotate this list working out some kind of basic score for each item
        # The simplest way is probably to len(str(item['_matchesInfo'])) which for the
        # above example returns a score of 386 and for the bottom result in my test is
        # just 40.

        # TODO: Implement `boost` on fields
        for item in results:
            item['score'] = len(str(item['_matchesInfo']))

        sorted_results = sorted(results, key=itemgetter('score'), reverse=True)
        sorted_ids = [_['id'] for _ in sorted_results]

        # This piece of utter genius is borrowed wholesale from wagtail-whoosh after I spent
        # several hours trying and failing to work out how to do this.
        if qc.order_by_relevance:
            # Retrieve the results from the db, but preserve the order by score
            preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(sorted_ids)])
            results = qc.queryset.filter(pk__in=sorted_ids).order_by(preserved_order)
        else:
            results = qc.queryset.filter(pk__in=sorted_ids)
        results = results.distinct()[self.start:self.stop]

        return results

    def _do_count(self):
        return len(self._do_search())


class MeiliSearchBackend(BaseSearchBackend):

    query_compiler_class = MeiliSearchQueryCompiler
    rebuilder_class = MeiliSearchRebuilder
    results_class = MeiliSearchResults

    def __init__(self, params):
        super().__init__(params)
        self.params = params
        try:
            self.client = meilisearch.Client(
                '{}:{}'.format(self.params['HOST'], self.params['PORT']),
                self.params['MASTER_KEY']
            )
        except Exception:
            raise

    def _refresh(self, uid, model):
        index = self.client.get_index(uid)
        index.delete()
        new_index = self.get_index_for_model(model)
        return new_index

    def get_index_for_model(self, model):
        return MeiliSearchModelIndex(self, model)

    def get_rebuilder(self):
        return None

    def reset_index(self):
        raise NotImplementedError

    def add_type(self, model):
        self.get_index_for_model(model).add_model(model)

    def refresh_index(self):
        refreshed_indexes = []
        for model in get_indexed_models():
            index = self.get_index_for_model(model)
            if index not in refreshed_indexes:
                index.refresh()
                refreshed_indexes.append(index)

    def add(self, obj):
        self.get_index_for_model(type(obj)).add_item(obj)

    def add_bulk(self, model, obj_list):
        self.get_index_for_model(model).add_items(model, obj_list)

    def delete(self, obj):
        self.get_index_for_model(type(obj)).delete_item(obj)

    def _search(self, query_compiler_class, query, model_or_queryset, **kwargs):
        # Find model/queryset
        if isinstance(model_or_queryset, QuerySet):
            model = model_or_queryset.model
            queryset = model_or_queryset
        else:
            model = model_or_queryset
            queryset = model_or_queryset.objects.all()

        # Model must be a class that is in the index
        if not class_is_indexed(model):
            return EmptySearchResults()

        # Check that theres still a query string after the clean up
        if query == "":
            return EmptySearchResults()

        # Search
        search_query = query_compiler_class(
            queryset, query, **kwargs
        )

        # Check the query
        search_query.check()

        return self.results_class(self, search_query)

    def search(
            self, query, model_or_queryset, fields=None, operator=None,
            order_by_relevance=True, partial_match=True):
        return self._search(
            self.query_compiler_class,
            query,
            model_or_queryset,
            fields=fields,
            operator=operator,
            order_by_relevance=order_by_relevance,
            partial_match=partial_match,
        )

    def autocomplete(self, query, model_or_queryset, fields=None, operator=None, order_by_relevance=True):
        if self.autocomplete_query_compiler_class is None:
            raise NotImplementedError("This search backend does not support the autocomplete API")

        return self._search(
            self.autocomplete_query_compiler_class,
            query,
            model_or_queryset,
            fields=fields,
            operator=operator,
            order_by_relevance=order_by_relevance,
        )


SearchBackend = MeiliSearchBackend
