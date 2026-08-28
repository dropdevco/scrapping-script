"""Export upcoming events to a Google Sheet that backs the GHL knowledge base.

The GHL bot used to be fed by a crawler pointed at the public events page, which
re-derived data this repo already stores in structured form and — worse — never
forgot: past events stayed indexed and the bot answered with concerts that had
already happened. This package writes the same rows straight out of Supabase
into a sheet GHL imports, rewriting it in full on every run so anything that has
already started simply stops existing.
"""
