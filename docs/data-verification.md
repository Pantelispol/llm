# Data verification queue

`data/pois.yaml` is a planning draft, not a claim that every value is current.
The catalog's `needs_verification` map is the machine-readable authority; this
document is the human review queue. Unknown hours are deliberately empty rather
than guessed. Verify time-sensitive values immediately before a production
release and retain evidence plus `verified_at`.

## 1. Opening hours and operational access

| poi_id | field | current value | why it needs verification | suggested official source |
|---|---|---|---|---|
| white_tower | opening_hours, last_entry_offset_minutes | unknown | Managed monument hours and last entry change seasonally. | [Museum of Byzantine Culture / White Tower](https://www.mbp.gr/en/) |
| rotunda | opening_hours, last_entry_offset_minutes | unknown | Monument access may differ from worship or event access. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| arch_of_galerius | opening_hours | `24/7` draft for exterior viewing | Confirm that public exterior access has no restrictions or works. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| roman_forum | opening_hours, last_entry_offset_minutes | unknown | Archaeological-site hours and closures are seasonal. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| hagios_demetrios | opening_hours, last_entry_offset_minutes | unknown | Services and feast days can change tourist access. | Holy Metropolis of Thessaloniki and the church office |
| acheiropoietos | opening_hours, last_entry_offset_minutes | unknown | Worship and monument access may use different schedules. | Holy Metropolis of Thessaloniki and [Ephorate](https://efapoth.gr/) |
| hagia_sophia | opening_hours, last_entry_offset_minutes | unknown | Services and religious observances affect visits. | Holy Metropolis of Thessaloniki and the church office |
| heptapyrgio | opening_hours, last_entry_offset_minutes | unknown | Managed-fortress hours and closed days can change. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| ano_poli_walls | opening_hours, last_entry_offset_minutes | unknown | Public viewpoints must be distinguished from managed wall interiors. | [Ephorate](https://efapoth.gr/) and Municipality of Thessaloniki |
| archaeological_museum | opening_hours conflict | supplied split: Apr 15–Nov 14 `08:00-20:00`; Nov 15–Apr 14 `09:00-16:00` | Assignment source wins for the demo, but a third party says April–October and current official pages may differ. | [Museum hours page](https://www.amth.gr/en/visit/hours-and-tickets) and [Discover Greece](https://www.discovergreece.com/experiences/tour-archaeological-museum-thessaloniki) |
| museum_of_byzantine_culture | opening_hours, last_entry_offset_minutes | unknown | Recent notices show schedules can change within a season. | [Official visit page](https://www.mbp.gr/en/visit/) |
| jewish_museum | opening_hours, last_entry_offset_minutes | unknown | Weekly split hours and last entry require a dated check. | [Jewish Museum of Thessaloniki](https://www.jmth.gr/) |
| bey_hamam | opening_hours, last_entry_offset_minutes | unknown | Interior access may be closed or event-dependent. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| aristotelous_square | opening_hours | `24/7` requirement-supplied public access | Confirm temporary closures and clarify that venue hours do not inherit this value. | Municipality of Thessaloniki |
| ladadika | opening_hours | `24/7` requirement-supplied area access | Individual businesses have independent schedules. | Municipality of Thessaloniki |
| kapani_market | opening_hours, last_entry_offset_minutes | unknown | Market-wide hours and stall hours may differ. | Municipality of Thessaloniki market administration |
| modiano_market | opening_hours, last_entry_offset_minutes | unknown | Hall and tenant schedules can differ. | [Agora Modiano](https://www.agoramodiano.com/) |
| new_waterfront_umbrellas | opening_hours | `24/7` requirement-supplied public access | Check works, event barriers, and night access conditions. | Municipality of Thessaloniki / Nea Paralia management |
| nea_paralia_parks | opening_hours | `24/7` requirement-supplied public access | Individual gardens or facilities may close independently. | Municipality of Thessaloniki / Nea Paralia management |
| seich_sou_forest | opening_hours, closure rules | unknown | Fire-risk, storm, maintenance, or civil-protection orders can override ordinary access. | Central Macedonia Civil Protection and Thessaloniki Forest Directorate |
| tsinari_ano_poli | opening_hours | `24/7` requirement-supplied area access | Individual venues have separate schedules. | Municipality of Thessaloniki |
| vlatadon_monastery | opening_hours, last_entry_offset_minutes | unknown | Services and monastery rules affect visitor access. | Vlatadon Monastery and Holy Metropolis of Thessaloniki |

## 2. Coordinates

All coordinates are representative points rounded to four decimals. They are
not suitable for routing until checked against the correct public entrance.

| poi_id | field | current value | why it needs verification | suggested official source |
|---|---|---|---|---|
| white_tower | coordinates | `40.6264, 22.9484` | Confirm the routable visitor entrance. | Official museum map / Hellenic Cadastre |
| rotunda | coordinates | `40.6321, 22.9529` | Confirm the visitor gate rather than the monument centroid. | Ephorate / Hellenic Cadastre |
| arch_of_galerius | coordinates | `40.6327, 22.9518` | Confirm the safest pedestrian approach point. | Ephorate / Municipality GIS |
| roman_forum | coordinates | `40.6378, 22.9455` | Confirm ticket entrance rather than site centroid. | Ephorate / Hellenic Cadastre |
| hagios_demetrios | coordinates | `40.6388, 22.9472` | Confirm accessible public entrance. | Church office / Municipality GIS |
| acheiropoietos | coordinates | `40.6345, 22.9475` | Confirm accessible public entrance. | Church office / Municipality GIS |
| hagia_sophia | coordinates | `40.6328, 22.9471` | Confirm accessible public entrance. | Church office / Municipality GIS |
| heptapyrgio | coordinates | `40.6444, 22.9633` | Confirm ticket gate and avoid routing to perimeter walls. | Ephorate / Hellenic Cadastre |
| ano_poli_walls | coordinates | `40.6439, 22.9605` | The walls span a large area; select an explicit viewpoint. | Ephorate / Municipality GIS |
| archaeological_museum | coordinates | `40.6258, 22.9540` | Confirm main visitor entrance. | [Official museum](https://www.amth.gr/en/) |
| museum_of_byzantine_culture | coordinates | `40.6238, 22.9543` | Confirm main visitor entrance. | [Official museum](https://www.mbp.gr/en/) |
| jewish_museum | coordinates | `40.6351, 22.9410` | Confirm entrance and current building access. | [Official museum](https://www.jmth.gr/) |
| bey_hamam | coordinates | `40.6361, 22.9446` | Confirm any currently usable public entrance. | Ephorate / Hellenic Cadastre |
| aristotelous_square | coordinates | `40.6323, 22.9409` | Choose a consistent meeting point within the large square. | Municipality GIS |
| ladadika | coordinates | `40.6350, 22.9365` | Area POI needs a documented representative point. | Municipality GIS |
| kapani_market | coordinates | `40.6361, 22.9430` | Confirm the preferred market entrance. | Municipality market administration |
| modiano_market | coordinates | `40.6349, 22.9419` | Confirm the preferred public entrance. | [Agora Modiano](https://www.agoramodiano.com/) |
| new_waterfront_umbrellas | coordinates | `40.6200, 22.9517` | Confirm the sculpture's pedestrian waypoint. | Municipality GIS |
| nea_paralia_parks | coordinates | `40.6119, 22.9537` | The parks span kilometres; choose a documented anchor point. | Nea Paralia management / Municipality GIS |
| seich_sou_forest | coordinates | `40.6477, 23.0054` | A forest centroid is unsafe for routing; select approved trailheads. | Thessaloniki Forest Directorate |
| tsinari_ano_poli | coordinates | `40.6416, 22.9509` | Area POI needs a documented meeting point. | Municipality GIS |
| vlatadon_monastery | coordinates | `40.6419, 22.9527` | Confirm public entrance and accessible approach. | Monastery / Hellenic Cadastre |

## 3. Remaining catalog fields

These grouped rows point to the machine-readable flags in `pois.yaml`; each
listed field has its own `needs_verification: true` entry unless the assignment
explicitly supplied it.

| poi_id | field | current value | why it needs verification | suggested official source |
|---|---|---|---|---|
| white_tower | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, source | draft values in catalog | Confirm taxonomy, accessibility, duration, and operational facts. | Museum of Byzantine Culture / White Tower |
| rotunda | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, source | draft values in catalog | Confirm visitor facilities and monument classification. | Ephorate of Antiquities of Thessaloniki |
| arch_of_galerius | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Exterior access and accessibility need an official check. | Ephorate / Municipality |
| roman_forum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, source | draft values in catalog | Site facilities and visit duration require confirmation. | Ephorate of Antiquities of Thessaloniki |
| hagios_demetrios | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Religious access and accessibility may differ by entrance. | Church office / Holy Metropolis |
| acheiropoietos | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Religious access and accessibility require confirmation. | Church office / Holy Metropolis |
| hagia_sophia | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Religious access and accessibility require confirmation. | Church office / Holy Metropolis |
| heptapyrgio | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Terrain and accessible routes have planning impact. | Ephorate of Antiquities of Thessaloniki |
| ano_poli_walls | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price, safety_tier, notes, source | draft values in catalog | The chosen segment determines slope, access, and safety. | Ephorate / Municipality |
| archaeological_museum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, conflict URL, source URL | draft values except supplied hours and last-entry offset | Confirm all non-supplied fields and reconcile changing live schedules. | [Official museum](https://www.amth.gr/en/) |
| museum_of_byzantine_culture | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, source | draft values in catalog | Verify current accessibility and practical visit duration. | [Official museum](https://www.mbp.gr/en/) |
| jewish_museum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, source | draft values in catalog | Verify security/accessibility arrangements and current prices. | [Official museum](https://www.jmth.gr/) |
| bey_hamam | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Confirm whether interior visits are presently possible. | Ephorate of Antiquities of Thessaloniki |
| aristotelous_square | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Verify accessibility and night-lighting assumptions. | Municipality of Thessaloniki |
| ladadika | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Area-level attributes vary by street and time. | Municipality of Thessaloniki |
| kapani_market | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Stall layout and accessibility can change. | Municipality market administration |
| modiano_market | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Confirm hall accessibility and tenant-independent facts. | [Agora Modiano](https://www.agoramodiano.com/) |
| new_waterfront_umbrellas | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Weather and night conditions affect suitability. | Municipality / Nea Paralia management |
| nea_paralia_parks | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Facilities vary across the long park system. | Municipality / Nea Paralia management |
| seich_sou_forest | Greek name, aliases, tags, visit_minutes, child_friendly, step_free, price, notes, source | draft values; category, exposure, heat and critical safety tier supplied | Trailhead, closure, fire-risk and accessibility data are safety-critical. | Forest Directorate / Central Macedonia Civil Protection |
| tsinari_ano_poli | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Area-level slope and accessibility require mapping. | Municipality of Thessaloniki |
| vlatadon_monastery | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price, safety_tier, notes, source | draft values in catalog | Confirm monastery rules, terrain, and visitor facilities. | Vlatadon Monastery / Holy Metropolis |

## Holiday review

Every entry in `data/holidays.yaml` is marked `needs_verification: true`.
Cross-check the 2026 dates and whether each observance closes the specific type
of venue. October 26 is local to Thessaloniki; it must not be applied nationally.
The engine must read this file directly in Phase 2b rather than trusting holiday
data embedded in the opening-hours dependency.

