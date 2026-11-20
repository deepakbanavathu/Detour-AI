-- osm2pgsql flex style for DetourAI.
-- Output: osm.highway       ways with the tags Module 2 needs and the ordered node list
--         osm.point_feature barriers, signals, traffic calming, crossings
--         osm.restriction   turn-restriction relations with members
--         osm.place_point   named nodes that can anchor an advisory: junctions, signals, places, landmarks
--         osm.place_area    named polygons: suburbs, neighbourhoods, landuse blocks, admin boundaries

local highway = osm2pgsql.define_way_table('highway', {
    { column = 'tags',  type = 'jsonb' },
    { column = 'nodes', type = 'text' },                 -- 'n1,n2,...' in way order; same order as geometry vertices
    { column = 'geom',  type = 'linestring', projection = 4326 },
}, { schema = 'osm' })

local point_feature = osm2pgsql.define_node_table('point_feature', {
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'point', projection = 4326 },
}, { schema = 'osm' })

local restriction = osm2pgsql.define_relation_table('restriction', {
    { column = 'tags',    type = 'jsonb' },
    { column = 'members', type = 'jsonb' },
}, { schema = 'osm' })

local place_point = osm2pgsql.define_node_table('place_point', {
    { column = 'name',  type = 'text' },
    { column = 'kind',  type = 'text' },                  -- JUNCTION | SIGNAL | PLACE | LANDMARK
    { column = 'tags',  type = 'jsonb' },
    { column = 'geom',  type = 'point', projection = 4326 },
}, { schema = 'osm' })

local place_area = osm2pgsql.define_table({
    name = 'place_area', schema = 'osm',
    ids = { type = 'any', id_column = 'osm_id', type_column = 'osm_type' },
    columns = {
        { column = 'name', type = 'text' },
        { column = 'kind', type = 'text' },               -- PLACE | LANDUSE | BOUNDARY
        { column = 'tags', type = 'jsonb' },
        { column = 'geom', type = 'multipolygon', projection = 4326 },
    },
})

local keep = {
    highway = true, name = true, ['name:kn'] = true, ['name:te'] = true, ['name:hi'] = true,
    alt_name = true, old_name = true, lanes = true, width = true, maxheight = true, maxwidth = true,
    maxweight = true, oneway = true, surface = true, access = true, motorcycle = true, motorcar = true,
    hgv = true, tunnel = true, bridge = true, layer = true, service = true, junction = true, lit = true,
    traffic_calming = true, maxspeed = true, ['lanes:forward'] = true, ['lanes:backward'] = true, ref = true,
    vehicle = true, motor_vehicle = true, goods = true, psv = true, bus = true,
}

local highway_ok = {
    motorway = true, motorway_link = true, trunk = true, trunk_link = true,
    primary = true, primary_link = true, secondary = true, secondary_link = true,
    tertiary = true, tertiary_link = true, unclassified = true, residential = true,
    living_street = true, service = true,
}

local place_ok = { suburb = true, neighbourhood = true, quarter = true, locality = true, village = true, town = true, city = true }
local landmark_keys = { 'amenity', 'tourism', 'historic', 'shop', 'office', 'building', 'railway', 'public_transport', 'aeroway', 'leisure' }

local function area_kind(t)
    if t.place and place_ok[t.place] then return 'PLACE' end
    if t.boundary == 'administrative' and t.admin_level and tonumber(t.admin_level) and tonumber(t.admin_level) >= 8 then return 'BOUNDARY' end
    if t.landuse and (t.landuse == 'residential' or t.landuse == 'commercial' or t.landuse == 'industrial' or t.landuse == 'retail') then return 'LANDUSE' end
    return nil
end

function osm2pgsql.process_node(object)
    local t = object.tags
    if t.barrier or t.highway == 'traffic_signals' or t.traffic_calming or t.highway == 'crossing' then
        point_feature:insert({ tags = t, geom = object:as_point() })
    end
    if t.name then
        local kind = nil
        if t.junction or t.highway == 'motorway_junction' then kind = 'JUNCTION'
        elseif t.highway == 'traffic_signals' then kind = 'SIGNAL'
        elseif t.place and place_ok[t.place] then kind = 'PLACE'
        else
            for _, k in ipairs(landmark_keys) do
                if t[k] then kind = 'LANDMARK'; break end
            end
        end
        if kind then
            place_point:insert({ name = t.name, kind = kind, tags = t, geom = object:as_point() })
        end
    end
end

function osm2pgsql.process_way(object)
    local t = object.tags
    local hw = t.highway
    if hw and highway_ok[hw] and t.area ~= 'yes' then
        local kept = {}
        for k, v in pairs(t) do
            if keep[k] then kept[k] = v end
        end
        highway:insert({
            tags  = kept,
            nodes = table.concat(object.nodes, ','),
            geom  = object:as_linestring(),
        })
        return
    end
    if t.name and object.is_closed then
        local kind = area_kind(t)
        if kind then
            place_area:insert({ name = t.name, kind = kind, tags = t, geom = object:as_multipolygon() })
        end
    end
end

function osm2pgsql.process_relation(object)
    local t = object.tags
    if t.type == 'restriction' then
        restriction:insert({ tags = t, members = object.members })
        return
    end
    if t.name and (t.type == 'multipolygon' or t.type == 'boundary') then
        local kind = area_kind(t)
        if kind then
            place_area:insert({ name = t.name, kind = kind, tags = t, geom = object:as_multipolygon() })
        end
    end
end
