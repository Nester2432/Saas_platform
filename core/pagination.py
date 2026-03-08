"""
core/pagination.py

Platform-wide pagination classes.

DefaultPagination is the standard paginator for all list endpoints.
It can be applied at the ViewSet level (preferred) or set globally
in REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"] in settings.py.

Response envelope produced by DRF PageNumberPagination:
    {
        "count":    150,          ← total records matching the query
        "next":     "...?page=3", ← null on last page
        "previous": "...?page=1", ← null on first page
        "results":  [...]         ← current page records
    }

Query params:
    ?page=2           → page number (1-indexed)
    ?page_size=50     → records per page (capped at max_page_size)
"""

from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    """
    Standard paginator for all business module list endpoints.

    Defaults:   25 records/page
    Override:   ?page_size=50
    Hard cap:   100 records/page (prevents accidental full-table responses)
    """
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class LargePagination(PageNumberPagination):
    """
    For endpoints that legitimately need larger pages (e.g. exports,
    dropdown autocomplete with many items).

    Use sparingly — large pages strain serialization and DB memory.
    """
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500


class SmallPagination(PageNumberPagination):
    """
    For nested list actions where fewer items are expected per page
    (e.g. GET /clientes/{id}/notas/, GET /clientes/{id}/historial/).
    """
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50
