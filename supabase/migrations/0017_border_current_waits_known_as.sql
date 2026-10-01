-- 0017_border_current_waits_known_as.sql — the names people actually call each bridge.
--
-- House standard (docs 07): additive only. One new column on a table that holds
-- nothing yet, with a default, so it is safe on a live database.
--
-- In the meeting of 2026-09-29 the bridges were "Centro", "Puente Libre", "Lerdo" and
-- "Zaragoza". border_current_waits named the first one "Paso del Norte (Santa Fe)" and
-- nowhere else, so a follower asking the GoHighLevel bot "¿cómo está Centro?" asked
-- about a word its knowledge base never contained. This column carries those names
-- ("Centro, Santa Fe, PDN"), and summary_es / summary_en now end with them too, since
-- the sentence is what the bot retrieves on.
--
-- NOT NULL like every text column here: GoHighLevel rejects a row with a blank cell.
-- border.poll always fills it; the default only exists so the column can be added.
alter table border_current_waits
    add column if not exists also_known_as text not null default 'Not listed';
