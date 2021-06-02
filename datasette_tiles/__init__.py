from datasette import hookimpl
from datasette.utils.asgi import Response, NotFound
from datasette_tiles.utils import (
    detect_mtiles_databases,
    tiles_stack_database_order,
    latlon_to_tile_with_adjust,
    tile_to_latlon,
)
import json
import math

# Empty vector tile from tilemaker
MVT_404 = b"\x1F\x8B\x08\x00\xFA\x78\x18\x5E\x00\x03\x93\xE2\xE3\x62\x8F\x8F\x4F\xCD\x2D\x28\xA9\xD4\x68\x50\xA8\x60\x02\x00\x64\x71\x44\x36\x10\x00\x00\x00"

SELECT_TILE_SQL = """
select
  tile_data
from
  tiles
where
  zoom_level = :z
  and tile_column = :x
  and tile_row = :y
""".strip()


@hookimpl
def register_routes():
    return [
        (r"/-/vector-tiles$", index),
        (r"/-/vector-tiles/(?P<db_name>[^/]+)$", explorer),
        (r"/-/vector-tiles/(?P<db_name>[^/]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.mvt$", tile),
        (
            r"/-/vector-tiles-tms/(?P<db_name>[^/]+)/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.mvt$",
            tile_tms,
        ),
        (r"/-/vector-tiles-stack$", tiles_stack_explorer),
        (r"/-/vector-tiles-stack/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.mvt$", tiles_stack),
        (r"/-/vector-tiles-stack-tms/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.mvt$", tiles_stack_tms),
    ]


async def index(datasette):
    return Response.html(
        await datasette.render_template(
            "tiles_index.html",
            {"mbtiles_databases": await detect_mtiles_databases(datasette)},
        )
    )


async def load_tile(db, request, tms):
    z = int(request.url_vars["z"])
    x = int(request.url_vars["x"])
    y = int(request.url_vars["y"])
    if not tms:
        y = int(math.pow(2, z) - 1 - y)
    result = await db.execute(
        SELECT_TILE_SQL,
        {
            "z": z,
            "x": x,
            "y": y,
        },
    )
    if not result.rows:
        return None
    return result.rows[0][0]


async def tile(request, datasette):
    return await _tile(request, datasette, tms=False)


async def tile_tms(request, datasette):
    return await _tile(request, datasette, tms=True)


async def _tile(request, datasette, tms):
    db_name = request.url_vars["db_name"]
    mbtiles_databases = await detect_mtiles_databases(datasette)
    if db_name not in mbtiles_databases:
        raise NotFound("Not a valid mbtiles database")
    db = datasette.get_database(db_name)
    tile = await load_tile(db, request, tms)
    if tile is None:
        return Response(body=MVT_404, content_type="application/vnd.mapbox-vector-tile", status=404, headers={'Content-Encoding': 'gzip'})
    return Response(body=tile, content_type="application/vnd.mapbox-vector-tile", headers={'Content-Encoding': 'gzip'})


async def _tiles_stack(datasette, request, tms):
    priority_order = await tiles_stack_database_order(datasette)
    # Try each database in turn
    for database in priority_order:
        tile = await load_tile(database, request, tms=tms)
        if tile is not None:
            return Response(body=tile, content_type="application/vnd.mapbox-vector-tile", headers={'Content-Encoding': 'gzip'})
    return Response(body=MVT_404, content_type="application/vnd.mapbox-vector-tile", status=404, headers={'Content-Encoding': 'gzip'})


async def tiles_stack(datasette, request):
    return await _tiles_stack(datasette, request, tms=False)


async def tiles_stack_tms(datasette, request):
    return await _tiles_stack(datasette, request, tms=True)


async def explorer(datasette, request):
    db_name = request.url_vars["db_name"]
    mbtiles_databases = await detect_mtiles_databases(datasette)
    if db_name not in mbtiles_databases:
        raise NotFound("Not a valid mbtiles database")
    db = datasette.get_database(db_name)
    metadata = {
        row["name"]: row["value"]
        for row in (await db.execute("select name, value from metadata")).rows
    }
    default_latitude = 0
    default_longitude = 0
    default_zoom = 0
    if metadata.get("center") and len(metadata["center"].split(",")) == 3:
        default_longitude, default_latitude, default_zoom = metadata["center"].split(
            ","
        )
    min_zoom = 0
    max_zoom = 19
    if metadata.get("minzoom"):
        min_zoom = int(metadata["minzoom"])
    if metadata.get("maxzoom"):
        max_zoom = int(metadata["maxzoom"])
    attribution = metadata.get("attribution") or None

    # Provided location data
    lat = float(request.args.get("lat", default_latitude))
    lon = float(request.args.get("lon", default_longitude))
    zoom = int(request.args.get("z", default_zoom))
    if zoom > max_zoom:
        zoom = max_zoom
    if zoom < min_zoom:
        zoom = min_zoom
    x_tile, y_tile = latlon_to_tile_with_adjust(lat, lon, zoom)

    return Response.html(
        await datasette.render_template(
            "tiles_explorer.html",
            {
                "nojs": request.args.get("nojs") or request.args.get("lat"),
                "metadata": metadata,
                "db_name": db_name,
                "db_path": datasette.urls.database(db_name),
                "default_latitude": default_latitude,
                "default_longitude": default_longitude,
                "default_zoom": default_zoom,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "attribution": json.dumps(attribution),
                "current_latitude": lat,
                "current_longitude": lon,
                "can_zoom_in": zoom < max_zoom,
                "can_zoom_out": zoom > min_zoom,
                "current_zoom": zoom,
                "current_x": x_tile,
                "current_y": y_tile,
                "compass": {
                    "n": tile_to_latlon(x_tile, y_tile - 1, zoom),
                    "s": tile_to_latlon(x_tile, y_tile + 1, zoom),
                    "e": tile_to_latlon(x_tile + 1, y_tile, zoom),
                    "w": tile_to_latlon(x_tile - 1, y_tile, zoom),
                },
            },
        )
    )


async def tiles_stack_explorer(datasette):
    attribution = ""
    # Find min/max zoom by looking at the stack
    priority_order = await tiles_stack_database_order(datasette)
    min_zooms = []
    max_zooms = []
    attributions = []
    for db in priority_order:
        metadata = {
            row["name"]: row["value"]
            for row in (await db.execute("select name, value from metadata")).rows
        }
        if "minzoom" in metadata:
            min_zooms.append(int(metadata["minzoom"]))
        if "maxzoom" in metadata:
            max_zooms.append(int(metadata["maxzoom"]))
    # If all attributions are the same, use that - otherwise leave blank
    if len(set(attributions)) == 1:
        attribution = attributions[0]
    min_zoom = min(min_zooms)
    max_zoom = max(max_zooms)
    return Response.html(
        await datasette.render_template(
            "tiles_stack_explorer.html",
            {
                "default_latitude": 0,
                "default_longitude": 0,
                "default_zoom": min_zoom,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "attribution": json.dumps(attribution),
            },
        )
    )


@hookimpl
def database_actions(datasette, database):
    async def inner():
        mbtiles_databases = await detect_mtiles_databases(datasette)
        if database in mbtiles_databases:
            return [
                {
                    "href": datasette.urls.path("/-/vector-tiles/{}".format(database)),
                    "label": "Explore these tiles on a map",
                }
            ]

    return inner


@hookimpl
def table_actions(datasette, database, table):
    async def inner():
        if table != "tiles":
            return None
        mbtiles_databases = await detect_mtiles_databases(datasette)
        if database in mbtiles_databases:
            return [
                {
                    "href": datasette.urls.path("/-/vector-tiles/{}".format(database)),
                    "label": "Explore these tiles on a map",
                }
            ]

    return inner
