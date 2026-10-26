-- 0020_border_tornillo_port.sql — Tornillo is CBP port 240401, not 240221.
--
-- House standard (docs 07): additive only. No drops, no deletes, no alterations of
-- existing columns. Safe to run against a live database with data, and to run twice.
--
-- 0013 seeded Tornillo as 240221. In the CBP feed that id is an "El Paso" entry with an
-- empty crossing_name that reads "Update Pending" on every lane, all day, so Tornillo
-- always read "no data". The real crossing is 240401, which the feed calls "Marcelino
-- Serna" / "Tornillo" and which carries real numbers: checked live on 2026-10-12 (30 min)
-- and 2026-10-26 (20 min, with 240221 still "Update Pending"). border.cbp reads 240401
-- from the same release (roadmap step 0.1).

-- ---------------------------------------------------------------------------
-- 1. The real crossing, with 0013's names, aliases and pin: those describe the place,
--    only the id was wrong. (The pin is carried over as 0013 had it, not re-surveyed.)
--
-- The poller also writes this table: BorderStore.upsert_ports puts name, name_es and
-- sort_order for every port in its registry. If it ran before this migration it left a
-- bare 240401 row (aliases '{}', no pin), so on conflict fill only what is still empty.
-- A value somebody set by hand is never clobbered, which is 0013's rule too.
-- ---------------------------------------------------------------------------
insert into border_ports (port_number, name, name_es, aliases, lat, lng, mx_side, sort_order) values
    ('240401', 'Tornillo–Guadalupe', 'Guadalupe–Tornillo', array['Tornillo','Guadalupe','Marcelino Serna'], 31.3906, -106.0870, 'Guadalupe, Chih.', 60)
on conflict (port_number) do update set
    aliases = case when border_ports.aliases = '{}' then excluded.aliases else border_ports.aliases end,
    lat     = coalesce(border_ports.lat, excluded.lat),
    lng     = coalesce(border_ports.lng, excluded.lng),
    mx_side = coalesce(border_ports.mx_side, excluded.mx_side);

-- ---------------------------------------------------------------------------
-- 2. Retire 240221 through the `active` flag 0013 gave the table, and keep the row:
--    border_readings and border_current_waits rows still reference it.
-- ---------------------------------------------------------------------------
update border_ports set active = false where port_number = '240221';
