# Sikkerhet og personvern

spond-familie behandler data om barn: navn, svar, kommentarer og meldinger. Dette er
valgene som er tatt for å holde det trygt.

## Hva som lagres hvor

| Hva | Hvor | Merknad |
|---|---|---|
| Spond-passordet ditt | **Ingen steder** | Brukes én gang av `spond-familie login`. |
| Spond-token | `~/.local/state/spond-familie/token.json` (0600) | Fornyes automatisk. Slett fila for å logge ut. |
| Logg over skrivinger | `~/.local/state/spond-familie/audit.jsonl` (0600) | Hver forhåndsvisning og utført handling. |
| Oppmøte-Excel | `~/Spond-eksport/` (0600) | Fila fra Spond inneholder kontaktdata. |
| Passfrase for fjerntilgang | `owner.json` (bare scrypt-hash) | Bare hvis du bruker `serve-http`. |

På Windows ligger tilstanden i `%LOCALAPPDATA%\spond-familie`. Mappen kan overstyres med
`SPOND_MCP_HOME`.

## Hva Claude får se

- Verktøyene bygger svarene fra hvitelister over felter. Telefon, e-post,
  fødselsdato og adresse går aldri ut, heller ikke for foresatte og kontaktpersoner.
  En egen test leter etter slike felt i alle leseverktøy.
- Id-er oversettes til navn. Tider vises i Oslo-tid.
- Svarlister med navn vises bare i grupper der du har en rolle, eller der Spond
  selv viser dem. I grupper der du bare er forelder respekteres «skjul deltakere».
- Det Claude ser, sendes til Anthropic som en del av samtalen. Det gjelder også
  navn på andre barn på laget. Bruk verktøyene til det du faktisk trenger.

## Skriving: alltid to steg

`rsvp` og `send_message` gjør ingenting første gang. De lager en forhåndsvisning
med en `preview_id`. Først et nytt kall med `confirm=true` og samme `preview_id`
utfører:

- Forhåndsvisningen er engangs, gyldig i 10 minutter og bundet til nøyaktig samme
  argumenter. En endret tekst eller et annet barn krever ny forhåndsvisning.
- Alle sjekker kjøres på nytt mot ferske data rett før utførelse.
- `rsvp` gjelder bare barn der du står som foresatt i Spond. Det avvises for
  avlyste og startede arrangementer, for påmelding som ikke er åpnet, og for barn
  som ikke er invitert. Etterpå sjekkes det at Spond faktisk viser det nye svaret.
- `send_message` er bare tillatt i grupper der du har en rolle (trener,
  lagleder, gruppestyrer …). Maks 4 mottakere, hver får en egen melding, og en
  sending som feiler prøves aldri på nytt (det kunne gitt dobbel melding).
- I Claude kan du i tillegg sette disse verktøyene til **Needs approval**.
  Gjør det.
- Vil du bare lese, setter du `SPOND_MCP_READONLY=1`, eller kjører
  `spond-familie setup-claude --readonly`. Da finnes ikke skriveverktøyene.

## Innlogging som varer

Spond gir et access-token (24 t) og et refresh-token (90 d). Hver fornying gir
et nytt par, og refresh-tokenet får 90 nye dager. Så lenge du bruker spond-familie
minst én gang i kvartalet, trenger du aldri logge inn igjen. Et brukt
refresh-token er dødt med én gang hos Spond, så alle prosesser på maskinen
deler ett lager og fornyer under fillås.

Mange mislykkede passordinnlogginger kan låse Spond-kontoen. spond-familie prøver
derfor aldri passord på nytt av seg selv. Etter en avvist innlogging venter den
10 minutter.

## Fjerntilgang (claude.ai og mobil)

`serve-http` er for deg som vil bruke spond-familie fra claude.ai og mobilen. Da blir
serveren nåbar fra internett:

- OAuth for én eier. Claude registrerer seg selv, men bare med Claudes egne
  tilbakekallsadresser eller loopback. Tilgang krever passfrasen din på
  innloggingssiden (5 feil gir 15 minutters sperre).
- Tjenesten bruker aldri Spond-passordet, bare tokenet, og nekter å starte hvis
  `SPOND_PASSWORD` er satt.
- `spond-familie revoke-all` logger ut alle tilkoblinger.

Se [fjerntilgang.md](fjerntilgang.md).

## Det uoffisielle API-et

Spond har ikke noe offentlig API. spond-familie bruker det samme som Spond sin egen
app, via det åpne [`spond`](https://github.com/Olen/Spond)-biblioteket. Det kan
endre seg uten varsel. Bruk det til egne barn og egne lag, ikke til å samle inn
eller dele andres data. Spond sine vilkår sier ikke noe eksplisitt om dette, og
dette er ikke juridisk rådgivning.
