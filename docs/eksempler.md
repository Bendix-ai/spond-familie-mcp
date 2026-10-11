# Spond-hverdagen: smertepunkter og hvordan Claude løser dem

Vi gikk gjennom omtaler av Spond-appen med 1–3 stjerner i App Store (Norge,
Storbritannia, USA, Sverige og Danmark) og Google Play (norsk og engelsk), totalt
551, samt [NRK](https://www.nrk.no/rogaland/ingen-varsler-fra-spond-appen-i-sommer_-_-det-er-ro-i-sjelen-1.16970927),
[Trustpilot](https://www.trustpilot.com/review/spond.com) og Spond sine
hjelpesider. Tallene er grove (temaene er talt med nøkkelord). Klager om feil,
innlogging og reklame er utelatt, fordi en assistent ikke kan løse dem.

Alt som **skriver** noe, krever at du bekrefter i Claude: svar i Spond,
meldinger, kalenderoppføringer, Drive-filer og Notion-sider.

---

## 1. «For mange varsler»: ca. 8 % av klagene

> «Det er altså et helvetes mas fra denne appen» (App Store, mai 2026)

Samme beskjed kommer både som push og e-post, og mye av det er småprat.

**Spør Claude:** «Hva må jeg faktisk gjøre noe med i Spond denne uka? Hopp over småprat.»
→ Claude leser arrangementer, innlegg og chat i alle grupper og trekker ut det som krever handling.

**Med Slack:** «…og send det til meg som DM hver mandag.»

## 2. «Ingen oversikt med flere barn»: ca. 9 %

> «Med flere barn oppleves denne appen rotete og kaotisk» (App Store, sep. 2025)

**Spør Claude:** «Lag en ukeplan for Emma og Jonas på tvers av alle lag, med oppmøtetid og sted.»
→ `overview` henter fra alle gruppene samtidig og ordner det per dag.

**Med Gmail:** «Lag et utkast til partneren min om hvem som kjører hvem i helga.»

## 3. «Finner ikke igjen informasjonen»: ca. 5 %, økende

> «Var det i innlegg, melding, kommentar på arrangement?» (App Store, jan. 2026)

Spond har ikke noe globalt søk, og vedlegg blir skjult.

**Spør Claude:** «Hva skrev treneren om drakter og dugnad i høst? Sjekk innlegg, kommentarer, vedlegg og chat.»
→ `list_posts`, `get_event` og `get_chat` til sammen virker som et søk på tvers.

**Med Notion:** «Lag en foreldre-wiki for G12 med de faste beskjedene: treningstider, utstyr, dugnad, kontakter.»

## 4. «Kalenderen»: ca. 7 %

> «Heller ingen ical-lenke som man kan abonnere på» (App Store, aug. 2026)

Synkroniseringen krever full kalendertilgang og fyller kalenderen.

**Med Google Calendar:** «Legg kampene til G12 de neste 8 ukene i kalenderen "Barnas idrett", med oppmøtetid som start.»
→ Du bestemmer hva som legges inn og i hvilken kalender, og Spond trenger ikke tilgang til kalenderen din.

## 5. «Glemmer å svare, går glipp av avlysninger»

> «…så mange oppdateringer at jeg glemmer å bekrefte deltagelse» (Google Play, mars 2026)

**Spør Claude:** «Hvilke invitasjoner har barna ikke svart på, og når går fristene ut?»
→ `overview` viser ubesvarte arrangementer med frist, og avlysninger for seg.

**Med Påminnelser / Todoist:** «Lag påminnelser dagen før hver frist.»

**Og så:** «Svar ja for Jonas på lørdag.» → Claude viser en forhåndsvisning og venter på ditt ja.

## 6. Trener: «Må mase på dem som ikke har svart»

> «remind every 24 hours until they reply» (App Store GB, des. 2025)

**Spør Claude:** «Hvem har ikke svart på de tre neste kampene? Skriv en vennlig påminnelse til foresatte.»
→ `get_event` per kamp, så `send_message` til hver foresatt. Du ser mottakere og tekst før noe sendes.

## 7. Trener: oppmøte og rapporter, ca. 3 %

> «rapportalternativer … i ett "view"» (App Store, mai 2024)

**Spør Claude:** «Svarprosent per spiller i G12 de siste 8 ukene, bare kamper.»
→ `attendance_stats` skiller mellom «svart ja» og «møtte» (som bare finnes der oppmøte er ført).

**Med Google Drive:** «Last ned oppmøte-Excel for siste kamp og legg den i mappa "Trenerteam".»

## 8. Betaling og fakturaer, ca. 7 % (delvis)

Mange klubber sender kontingent og lisens via e-post (for eksempel Rubic), ikke Spond.

**Med Gmail:** «Finn kontingent- og klubbfakturaer i Gmail de siste 3 månedene og sammenlign med det Spond-innleggene sier om betaling.»
→ Løser det bare delvis, fordi spond-familie ikke leser betalinger i Spond.

## 9. Koordinering mellom foresatte

> «kona holder styr på Svømming mens jeg holder styr på Speideren» (Google Play, mars 2026)

**Spør Claude:** «Hva har skjedd i Spond siden i går som partneren min bør vite om? Skriv det som en kort melding.»

---

## Hvordan koble til andre connectors

- **claude.ai / Claude-appen:** Innstillinger → Connectors. Google Calendar, Gmail,
  Google Drive, Notion og Slack finnes ferdige. Slå på de du vil bruke i samtalen
  via **+ → Connectors**.
- **Claude Desktop:** de samme, pluss lokale MCP-servere (for eksempel Apple
  Påminnelser og Kalender) i `claude_desktop_config.json`.
- **Claude Code:** `claude mcp add …`.

## Det spond-familie *ikke* gjør

- Endrer ikke arrangementer, betalinger eller grupper.
- Sender ikke meldinger til hele grupper (bare til enkeltpersoner, maks 4 om gangen).
- Løser ikke feil i selve Spond-appen.
