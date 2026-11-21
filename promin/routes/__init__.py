"""The pages and the API, one module per subject. Each module has a `router`."""
from . import folders, links, login, pairing, queue, remote, settings, tvtest, users, videos

ROUTERS = tuple(
    module.router
    for module in (login, pairing, links, remote, users, settings, tvtest, videos, folders, queue)
)
