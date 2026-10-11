# spond-familie-mcp

**Koble Claude til din egen Spond.** Få én oversikt over alle barna og lagene,
svar på invitasjoner (med bekreftelse) og bruk trenerverktøy, rett fra Claude
Desktop, Claude Code eller claude.ai.

> Uoffisiell. Ikke tilknyttet eller godkjent av Spond AS. Bygger på det åpne
> [`spond`](https://github.com/Olen/Spond)-biblioteket.

```
Du:     Hva må jeg gjøre noe med i Spond denne uka?
Claude: Tre ting:
        • Emma har ikke svart på cupen lørdag. Fristen er i morgen kl. 12.
        • Jonas: treningen onsdag er flyttet til 17:30 (hall B).
        • Fotball G12 søndag er avlyst (banen er stengt).
        Vil du at jeg svarer for Emma?
```

## Hvorfor

Vi gikk gjennom 551 lavt rangerte omtaler av Spond-appen (App Store og Google
Play, 2024–2026). De vanligste klagene som handler om hverdagen var for mange
varsler, ingen samlet oversikt når man har flere barn og lag, at man ikke finner
igjen informasjon, og at kalenderen er vanskelig. Det er nettopp det en
assistent er god på. [Se smertepunktene og hvordan de løses →](docs/eksempler.md)

## Hva du kan spørre om

- «Hva skjer for barna denne uka? Bare det jeg må gjøre noe med.»
- «Lag en ukeplan for Emma og Jonas med oppmøtetid og sted.»
- «Hvilke invitasjoner har vi ikke svart på, og når går fristene ut?»
- «Svar nei for Emma på treningen onsdag, med melding "Fotballkamp".»
- «Hva skrev treneren om drakter og dugnad i høst? Sjekk innlegg, kommentarer og chat.»
- Trener: «Hvem har ikke svart på kampen lørdag?» og «Svarprosent per spiller siste 90 dager.»
- Med andre connectors: «Legg kampene inn i Google-kalenderen med oppmøtetid som start»,
  «Lag påminnelser dagen før fristene», «Lagre oppmøte-Excel i Drive».

## Installasjon (ca. 5 minutter)

Du trenger [Claude Desktop](https://claude.ai/download) (eller Claude Code) og
en Mac eller Linux-maskin. Windows bør virke, men er ikke testet.

**1. Installer `uv`** (verktøyet som installerer Python-programmer). Åpne
Terminal og lim inn:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Lukk Terminal og åpne den igjen.

**2. Installer spond-familie-mcp:**

```bash
uv tool install git+https://github.com/Bendix-ai/spond-familie-mcp
```

**3. Logg inn i Spond** (én gang). Passordet lagres ikke, bare et token som
fornyer seg selv. Bruker du SMS-kode i Spond, blir du spurt om den.

```bash
spond-familie login
```

**4. Koble til Claude Desktop:**

```bash
spond-familie setup-claude
```

Avslutt Claude Desktop helt (⌘Q) og åpne den igjen.

**5. Prøv:** Start en ny samtale og spør *«Hva skjer for barna denne uka?»*.
Claude spør om lov før den bruker verktøyene første gang.

<details>
<summary><b>Claude Code, bare lesing, oppdatering og avinstallering</b></summary>

```bash
# Claude Code
claude mcp add --scope user spond -- "$(which spond-familie)"

# Bare lesing (ingen svar eller meldinger)
spond-familie setup-claude --readonly

# Status, og feilsøking uten Claude
spond-familie status
spond-familie tool overview '{"days": 3}'

# Oppdater
uv tool upgrade spond-familie-mcp

# Fjern (og slett innloggingen)
uv tool uninstall spond-familie-mcp
rm -rf ~/.local/state/spond-familie
```

Loggen fra Claude Desktop finner du i `~/Library/Logs/Claude/mcp-server-spond.log`.
</details>

## Verktøyene

| Verktøy | Hva det gjør |
|---|---|
| `overview` | Hva skjer de neste dagene i alle grupper, hva barna ikke har svart på, og svarfrister |
| `list_events`, `get_event` | Arrangementer med oppmøtetid, sted, svar, kommentarer, vedlegg og koblet chat |
| `list_posts`, `list_chats`, `get_chat` | Innlegg og meldinger, med navn i stedet for id-er |
| `list_groups`, `find_person` | Grupper, roller og personer (aldri telefon eller e-post) |
| `rsvp` | Svar ja/nei for barna dine, med valgfri melding ved nei. **To steg: forhåndsvisning, så bekreftelse** |
| `attendance_stats` | Trener: svarprosent per spiller (og oppmøte der det er ført) |
| `export_attendance_xlsx` | Trener: Sponds oppmøte-Excel til `~/Spond-eksport` |
| `send_message` | Trener: melding til spiller eller foresatte. **To steg** |
| `status` | Innlogging og helse |

«Barna dine» er de du står som foresatt for i Spond. «Trener» betyr at du har en
rolle i gruppa (trener, lagleder, gruppestyrer …). Ingen konfigurasjon trengs.

## Sammen med andre connectors

Claude kan bruke flere connectors i samme samtale. Spond alene gir oversikt. Med
for eksempel Google Calendar, Gmail, Notion, Google Drive eller Påminnelser kan
Claude også legge kamper i familiekalenderen, lage påminnelser før fristene,
finne klubbfakturaer i e-posten eller bygge en foreldre-wiki fra innleggene.
[Eksempler →](docs/eksempler.md)

## På mobil og claude.ai

Claude Desktop og Claude Code kjører spond-familie på din egen maskin, så der
trengs ingen server. claude.ai og Claude-appen på mobilen kobler seg derimot til
fra Anthropics sky, og da må serveren ha en offentlig HTTPS-adresse. Det går fint
med en maskin hjemme som alltid står på (en Mac, Raspberry Pi eller NAS) og
Tailscale Funnel. Selve telefonen kan ikke være server.
[Veiledning →](docs/fjerntilgang.md)

## Sikkerhet

- Passordet ditt lagres aldri. Tokenet ligger i `~/.local/state/spond-familie` (0600).
- Svar og meldinger krever alltid forhåndsvisning og en eksplisitt bekreftelse.
  Det sjekkes på nytt rett før noe sendes, og alt logges.
- Kontaktdata (telefon, e-post, adresse, fødselsdato) sendes aldri til Claude.
- Det Claude ser, er en del av samtalen din med Anthropic. Det gjelder også navn
  på andre barn på laget.

[Mer om sikkerhet og personvern →](docs/sikkerhet.md)

## Andre Spond-MCP-er

Andre har laget lignende ting, og det er verdt å se på dem:
[Starefossen/spond-no-match-mcp](https://github.com/Starefossen/spond-no-match-mcp),
[quaat/spond_mcp](https://github.com/quaat/spond_mcp),
[kristoffervp/spond-mcp](https://github.com/kristoffervp/spond-mcp),
[wabiloo/spond-mcp](https://github.com/wabiloo/spond-mcp) og
[gr9driver/spond-mcp-server](https://github.com/gr9driver/spond-mcp-server).

Denne skiller seg ut med:
- familieoversikt på tvers av barn og lag, med frister
- svar med avmeldingsgrunn
- innlogging som varer uten lagret passord
- fjerntilgang for claude.ai og mobil med egen innlogging
- dokumentasjon på norsk

## Utvikling

```bash
git clone https://github.com/Bendix-ai/spond-familie-mcp && cd spond-familie-mcp
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
```

Testene kjører uten nettverk mot syntetiske data. [Notater om Spond-API-et →](docs/spond-api.md)

## Lisens

GPL-3.0-or-later, fordi `spond`-biblioteket er GPL-3.0. Se [LICENSE](LICENSE).
