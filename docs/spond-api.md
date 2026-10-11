# Notater om Spond sitt (uoffisielle) API

Målt mot ekte data i 2026. Nyttig hvis du vil utvide spond-familie. Alle id-er er
32-tegns hex-strenger. Tider er ISO-8601 i UTC (`2026-10-14T15:00:00Z`).

## Innlogging

- `POST core/v1/auth2/login {"email", "password"}` →
  `{accessToken, refreshToken, passwordToken}`, hver `{token, expiration}`.
  Access varer 24 t, refresh 90 d.
- `POST core/v1/auth2/login/refresh {"value": <refresh>}` (uten bearer) → nytt
  access **og** refresh. Refresh-utløpet glir 90 dager fram ved hver fornying.
  Funnet i Spond sin web-klient.
- Et brukt refresh-token gir **401 med én gang** (ingen nådeperiode), men dreper
  ikke de andre i samme familie. Nye passordinnlogginger påvirker ikke
  eksisterende familier.
- Valgfri SMS-2FA: login svarer da `{token, phoneNumber}`, så
  `POST auth2/2fa/verify {token, code, phoneNumber}`.
- Mislykkede innlogginger kan låse kontoen (`outOfLoginAttempts`).
- `spond==1.2.1` fornyer aldri selv, og logger inn med passord hver gang tokenet
  mangler. spond-familie ruter bibliotekets `login()` via token-lageret.

## Grupper og roller

- `GET core/v1/groups/` gir medlemmer, `roles`, `subGroups` og `contactPerson`.
  Både `contactPerson` og medlemsfeltene har kontaktdata.
- `group.roles = [{id, name, permissions[]}]`, og hvert medlem har
  `roles = [role_id]`.
- Du selv er medlemmet der `member.profile.id == GET profile → id`.
- Dine barn er medlemmene der en `guardians[].profile.id` er din profil.
- Bare barnets **medlems-id** forekommer i svarlistene, aldri foresattes.

## Arrangementer

- `GET core/v1/sponds/?groupId=…&minStartTimestamp=…` (biblioteket runder
  `min/maxStart` ned til midnatt UTC, så hent med margin og filtrer selv).
  Maks ca. 300 per kall, så store perioder hentes i 70-dagersvinduer.
- `GET core/v1/sponds/{id}` gir samme form som listeelementene, minus `comments`.
- `responses`: `acceptedIds, declinedIds, unansweredIds, waitinglistIds,
  unconfirmedIds`, pluss `declineMessages` (medlem → grunn) og
  `registeredAttendance` (bare når oppmøte er ført, se `registered`).
- `rsvpDate` er svarfristen, og er bare satt når arrangøren har lagt inn en.
  `inviteTime` i fremtiden betyr at invitasjonen ikke er sendt ennå, og da kan du
  ikke svare.
- `cancelled` finnes **bare** når den er `true`. Sjekk med `.get("cancelled") is True`.
- `meetupPrior` er minutter før start. 5 er standardverdien, og den er ofte meningsløs.
- `participantsHidden` betyr at en tom svarliste ikke betyr «ingen».
- `matchEvent` settes sjelden. Mange lag skriver bare kamper som «Lag A – Lag B».
- `autoAccept` betyr at alle står som «ja» til de melder seg av.
- Kommentarer: `get_events` gir alltid tomt `comments`. De ekte ligger på
  `GET core/v1/sponds/{id}/comments` (svar i `children`, ikke `replies`).

## Svar (RSVP)

`PUT core/v1/sponds/{id}/responses/{medlems-id}` med `{"accepted": true|false}`,
og `"declineMessage": "…"` bare når den ikke er tom (samme form som web-klienten).
Svaret er arrangementets oppdaterte `responses`.

## Chat

- `POST core/v1/chat` (krever `content-type: application/json`) → `{url, auth}`.
  Chat-kallene bruker header `auth: <auth>`, ikke bearer.
- `GET {url}/chats/?max=N` gir chatter med `participants` (profil-id-er),
  `groupId`, `type` (`DM | GROUP | CLUB_MESSAGE`) og siste melding.
- `GET {url}/chats/{id}/messages` gir tråden.
- Koblingen chat → arrangement ligger i `message.spond.id`.
- Sending: `POST {url}/messages` med `{text, type:"TEXT", recipient, groupId}` (ny)
  eller `{chatId, text, type:"TEXT"}` (svar). Biblioteket glemmer `await` ved svar
  i eksisterende chat, så spond-familie har egen kode.

## Oppmøte-eksport

`GET core/v1/sponds/{id}/export` gir en ekte xlsx med arkene «For print» og «For
import». «For import» har én rad per medlem: Status, Navn, E-post, Mobilnummer,
Foresatt 1–3 med navn, e-post og mobil, og Svartidspunkt. Statusverdier:
«Kommer», «Kan ikke komme», «Ikke svart». **Fila inneholder full kontaktinfo.**
