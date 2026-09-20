# Data verification queue

`data/pois.yaml` is a planning draft, not a claim that every value is current.
The catalog's `needs_verification` map is the machine-readable authority; this
document is the human review queue. Unknown hours are deliberately empty rather
than guessed. Verify time-sensitive values immediately before a production
release and retain evidence plus `verified_at`.

## 1. Opening hours and operational access

| poi_id | field | current value | why it needs verification | suggested official source |
|---|---|---|---|---|
| white_tower | opening_hours, temporary_closures | **verified 2026-09-20**: Apr–Oct `08:00-21:00`; Nov–Mar `08:30-15:30`; closed Oct 12–15, 2026 for construction | Re-check only when the official source becomes stale or announces a change. | [Museum of Byzantine Culture / White Tower](https://www.mbp.gr/en/) |
| white_tower | last_entry_before_close_min | `20` | The value is supported by the museums directorate but remains flagged pending direct venue confirmation. | [Museums directorate](https://archaeologicalmuseums.gr/en/museum/5df34af3deca5e2d79e8c155/white-tower-exhibition) |
| rotunda | opening_hours | **manually verified 2026-09-20, medium confidence**: Mon–Sat `08:00-20:00`; Sun closed | Google Maps listing is not an official operational API; refresh before production use. Last-entry remains unknown. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| arch_of_galerius | opening_hours | **classified for Phase 4c**: `24/7` exterior public-space access | This applies only to viewing the outdoor arch from the public space; it does not imply managed interior access, staffing, or lighting. | Municipality of Thessaloniki / [Ephorate](https://efapoth.gr/) |
| roman_forum | opening_hours | **manually verified 2026-09-20, medium confidence**: Mon and Wed–Sat `09:00-16:00`; Tue and Sun closed | Refresh against the official culture service before production use. Last-entry remains unknown. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| hagios_demetrios | opening_hours | **manually verified 2026-09-20, medium confidence**: Mon–Sat `06:00-22:00`; Sunday explicitly unknown | Active-church services can change access. Unknown Sunday is not encoded as closed and is excluded from plans. | Holy Metropolis of Thessaloniki and the church office |
| acheiropoietos | opening_hours | **manually verified 2026-09-20, medium confidence**: weekday, Saturday, and Sunday split morning/evening periods | The midday closure is operationally significant; services can still affect access. Last-entry remains unknown. | Holy Metropolis of Thessaloniki and [Ephorate](https://efapoth.gr/) |
| hagia_sophia | opening_hours | **manually verified 2026-09-20, medium confidence**: daily `07:00-21:00` | Active-church services and observances can change access. Last-entry remains unknown. | Holy Metropolis of Thessaloniki and the church office |
| heptapyrgio | opening_hours | **manually verified 2026-09-20, medium confidence**: daily `08:30-15:30` | Refresh against the official culture service before production use. Last-entry remains unknown. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| ano_poli_walls | opening_hours | **classified for Phase 4c**: `24/7` exterior public-space access | This describes the public exterior viewpoints and paths only; managed wall interiors remain out of scope. | [Ephorate](https://efapoth.gr/) and Municipality of Thessaloniki |
| archaeological_museum | opening_hours, date_overrides, closed, price_eur | **verified 2026-09-20**: Apr–Oct daily `09:00-17:00`; Nov–Mar Tue off, otherwise `09:00-17:00`; 20-minute last entry; standard €10/reduced €5 | The official museum replaces both stale sources. Preserve them as incident evidence, not planning input. | [Official museum hours](https://www.amth.gr/en/visit/hours-and-tickets) |
| archaeological_museum | conflicts | Discover Greece: Apr 15–Nov 14 `08:00-20:00`, remainder `09:00-16:00`; travel blog: Apr–Oct `08:00-20:00` | Both stale third-party schedules could send a visitor at 18:00, one hour after official closing. | [Discover Greece](https://www.discovergreece.com/experiences/tour-archaeological-museum-thessaloniki); [travel PDF](https://bucketlisttraveltrips.com/wp-content/uploads/2023/06/Thessaloniki-Downloadable-Guide.pdf) |
| museum_of_byzantine_culture | opening_hours | **partly verified 2026-09-20**: May 8–Oct 31 `08:00-20:00`; Nov–Mar Tue off, otherwise `08:30-15:30`; Apr 1–May 7 unknown | The uncovered spring period prevents the whole field from being marked verified. | [Museum of Byzantine Culture](https://www.mbp.gr/en/) |
| museum_of_byzantine_culture | last_entry_before_close_min | `20` | Secondary official museums directory supports it; direct venue confirmation is still requested. | [Museums directorate](https://archaeologicalmuseums.culture.gov.gr/en/museum/5df34af3deca5e2d79e8c154/museum-of-byzantine-culture) |
| jewish_museum | opening_hours | **conservative schedule recorded 2026-09-20, medium confidence**: Tue–Fri `09:00-14:00`, additional Wed `17:00-20:00`, Sun `10:00-14:00`, Sat closed; Monday unknown | [JMTh](https://www.jmth.gr/) and [JCT](https://www.jct.gr/) include Monday, while Google Maps reports it closed. Monday is excluded rather than guessed. Last-entry remains unknown. | [Jewish Museum of Thessaloniki](https://www.jmth.gr/) |
| bey_hamam | opening_hours, last_entry_before_close_min | unknown | Interior access may be closed or event-dependent. | [Ephorate of Antiquities of Thessaloniki](https://efapoth.gr/) |
| aristotelous_square | opening_hours | `24/7` requirement-supplied public access | Confirm temporary closures and clarify that venue hours do not inherit this value. | Municipality of Thessaloniki |
| ladadika | opening_hours | `24/7` requirement-supplied area access | Individual businesses have independent schedules. | Municipality of Thessaloniki |
| kapani_market | opening_hours | **manually verified 2026-09-20, medium confidence**: Mon/Wed/Sat `08:00-16:00`; Tue/Thu/Fri `08:00-21:00`; Sun closed | Market-wide hours and individual stall hours may differ. Last-entry remains unknown. | Municipality of Thessaloniki market administration |
| modiano_market | opening_hours | **manually verified 2026-09-20, low confidence**: Mon–Sat `08:00-24:00`; Sun `10:00-24:00` | Google Maps' Greek “12:00 π.μ.” means midnight, not noon. The venue reportedly reopened in September 2026, so hours may change and tenant hours can differ. | [Agora Modiano](https://www.agoramodiano.com/) |
| new_waterfront_umbrellas | opening_hours | `24/7` requirement-supplied public access | Check works, event barriers, and night access conditions. | Municipality of Thessaloniki / Nea Paralia management |
| nea_paralia_parks | opening_hours | `24/7` requirement-supplied public access | Individual gardens or facilities may close independently. | Municipality of Thessaloniki / Nea Paralia management |
| seich_sou_forest | opening_hours, closure rules | unknown | Fire-risk, storm, maintenance, or civil-protection orders can override ordinary access. | Central Macedonia Civil Protection and Thessaloniki Forest Directorate |
| tsinari_ano_poli | opening_hours | `24/7` requirement-supplied area access | Individual venues have separate schedules. | Municipality of Thessaloniki |
| vlatadon_monastery | opening_hours, last_entry_before_close_min | unknown | Services and monastery rules affect visitor access. | Vlatadon Monastery and Holy Metropolis of Thessaloniki |

Hours remain unverified or only partly verified for
`museum_of_byzantine_culture`, `bey_hamam`, `seich_sou_forest`, and
`vlatadon_monastery`. The White Tower and the Museum of Byzantine Culture also
retain unverified last-entry values. All other remaining field-level work is
listed below and remains authoritative through the catalog's
`needs_verification` flags.

### Phase 4c classification audit

The audit changed three operational classifications supplied by the assignment:

- `arch_of_galerius`: category changed from archaeological monument to outdoor
  `public_space`; exterior access remains `24/7`.
- `ano_poli_walls`: the existing outdoor `public_space` classification now has
  explicit `24/7` exterior access and zero last-entry offset.
- `white_tower`: exposure changed from mixed to indoor so rain repair treats the
  museum visit as sheltered.

The other catalog entries were reviewed for the same public-space/hours mismatch.
No other opening hours were changed: unknown managed-site, forest, church, and
monastery hours remain unknown rather than being inferred.

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
| white_tower | Greek name, aliases, category, tags, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Confirm taxonomy, accessibility, duration, and price. | Museum of Byzantine Culture / White Tower |
| rotunda | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Confirm visitor facilities and monument classification. | Ephorate of Antiquities of Thessaloniki |
| arch_of_galerius | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Exterior accessibility and conditions need an official check. | Ephorate / Municipality |
| roman_forum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Site facilities and visit duration require confirmation. | Ephorate of Antiquities of Thessaloniki |
| hagios_demetrios | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Religious access and accessibility may differ by entrance. | Church office / Holy Metropolis |
| acheiropoietos | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes | draft values in catalog | Religious access and accessibility require confirmation. | Church office / Holy Metropolis |
| hagia_sophia | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes | draft values in catalog | Religious access and accessibility require confirmation. | Church office / Holy Metropolis |
| heptapyrgio | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price_eur, safety_tier, notes | draft values in catalog | Terrain and accessible routes have planning impact. | Ephorate of Antiquities of Thessaloniki |
| ano_poli_walls | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | The chosen segment determines slope, access, and safety. | Ephorate / Municipality |
| archaeological_museum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, safety_tier, notes | draft values outside corrected operational fields | Confirm descriptive and accessibility fields separately from official operational data. | [Official museum](https://www.amth.gr/en/) |
| museum_of_byzantine_culture | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Verify current accessibility and practical visit duration. | [Official museum](https://www.mbp.gr/en/) |
| jewish_museum | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Verify security/accessibility arrangements and current prices. | [Official museum](https://www.jmth.gr/) |
| bey_hamam | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Confirm whether interior visits are presently possible. | Ephorate of Antiquities of Thessaloniki |
| aristotelous_square | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Verify accessibility and night-lighting assumptions. | Municipality of Thessaloniki |
| ladadika | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Area-level attributes vary by street and time. | Municipality of Thessaloniki |
| kapani_market | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes | draft values in catalog | Stall layout and accessibility can change. | Municipality market administration |
| modiano_market | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier | draft values in catalog | Confirm hall accessibility and tenant-independent facts. | [Agora Modiano](https://www.agoramodiano.com/) |
| new_waterfront_umbrellas | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Weather and night conditions affect suitability. | Municipality / Nea Paralia management |
| nea_paralia_parks | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Facilities vary across the long park system. | Municipality / Nea Paralia management |
| seich_sou_forest | Greek name, aliases, tags, visit_minutes, child_friendly, step_free, price_eur, notes, source | draft values; category, exposure, heat and critical safety tier supplied | Trailhead, closure, fire-risk and accessibility data are safety-critical. | Forest Directorate / Central Macedonia Civil Protection |
| tsinari_ano_poli | Greek name, aliases, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Area-level slope and accessibility require mapping. | Municipality of Thessaloniki |
| vlatadon_monastery | Greek name, aliases, category, tags, exposure, visit_minutes, child_friendly, step_free, hilly, heat_exposure, price_eur, safety_tier, notes, source | draft values in catalog | Confirm monastery rules, terrain, and visitor facilities. | Vlatadon Monastery / Holy Metropolis |

## Holiday review

Every entry in `data/holidays.yaml` is marked `needs_verification: true`.
Cross-check the 2026 dates and whether each observance closes the specific type
of venue. October 26 is local to Thessaloniki; it must not be applied nationally.
The engine must read this file directly in Phase 2b rather than trusting holiday
data embedded in the opening-hours dependency.

## Phase 5 content review queue

- `white_tower — What to see — the interior presentation introduces successive
  periods of Thessaloniki's history — exhibition scope can change and should be
  checked against the museum's current curatorial description.`
- `roman_forum — What to see — subterranean passages are part of what visitors
  can examine — the surviving feature is established, but present visitor
  visibility and interpretation need official confirmation.`
- `hagios_demetrios — What to see — the crypt preserves structures associated
  with Roman baths — the relationship should be checked against the church or
  Ephorate's preferred archaeological wording.`
- `acheiropoietos — What to see — floor remains are visible in the church — the
  extent presently visible to an ordinary visitor needs confirmation.`
- `modiano_market — History — restoration retained the market identity while
  adapting the interior for contemporary vendors — this characterization of
  the recent reuse should be reviewed against Agora Modiano's official history.`
- `tsinari_ano_poli — History — the neighborhood name is associated with its
  Ottoman past — the etymological connection was kept general because a precise
  derivation needs a local historical source.`
- `vlatadon_monastery — What to see — publicly accessible parts of the elevated
  complex provide views toward the Thermaic Gulf — sightlines and public access
  should be confirmed by the monastery.`
