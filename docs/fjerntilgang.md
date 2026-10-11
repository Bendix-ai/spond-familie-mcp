# Fjerntilgang: bruk spond-familie fra claude.ai og mobilen

**Trenger du dette?** Bare hvis du vil bruke Spond fra claude.ai i nettleseren
eller fra Claude-appen på mobilen. Claude Desktop og Claude Code kjører
spond-familie på din egen maskin og trenger ingenting av dette.

## Hvorfor det krever en server

claude.ai og mobilappen kobler seg til connectors fra Anthropics sky, ikke fra
telefonen din. Serveren må derfor ha:

- en **offentlig HTTPS-adresse på port 443**. I vår test kom claude.ai aldri
  gjennom på port 8443.
- et vertsnavn der **alle** DNS-oppslag gir offentlige IPv4-adresser. Private
  adresser og Tailscale-adresser (100.x) avvises.
- **innlogging**, siden serveren har tilgang til Spond-kontoen din.
  spond-familie har innebygd OAuth for én eier, beskyttet med en passfrase.

## Hvor kan den kjøre?

Den trenger en maskin som alltid står på.

| Alternativ | Ca. kostnad/mnd | Data hjemme? | Merknad |
|---|---|---|---|
| Mac som alltid står på (f.eks. Mac mini) | 5 kr i strøm | Ja | Slå av dvale. launchd-mal i `deploy/launchd/` |
| Raspberry Pi 4/5 | 3–5 kr i strøm + kjøp | Ja | Billigst og enklest. systemd-mal i `deploy/systemd/` |
| NAS (f.eks. Synology) | ~0 hvis den står på uansett | Ja | Tailscale-pakke eller Docker |
| Liten skyserver (Fly.io, Hetzner) | ca. 25–80 kr | Nei | Gir HTTPS selv. Dataene ligger hos leverandøren |

**Telefonen kan ikke være server.** iOS stopper apper kort tid etter at de går i
bakgrunnen, og Tailscale-appen på iPhone kan ikke tilby tjenester (Funnel krever
Tailscale-kommandolinjen, som ikke finnes på iOS). På Android går det bare med
hacks i Termux, som Tailscale ikke støtter.

## Oppsett med Tailscale Funnel (anbefalt)

[Tailscale](https://tailscale.com) er gratis for privat bruk. Funnel gir maskinen
din en offentlig HTTPS-adresse, `https://<maskin>.<tailnet>.ts.net`, uten at du
åpner noe i ruteren.

1. **Installer og logg inn** som i [README](../README.md) (`spond-familie login`).
2. **Velg en passfrase for fjerntilgang.** Bruk en lang, tilfeldig en fra
   passordbehandleren din:
   ```bash
   spond-familie owner-setup
   ```
3. **Finn adressen din:** `tailscale status --self` viser navnet,
   f.eks. `min-mac.tail1234.ts.net`.
4. **Start tjenesten.** Kopier malen for systemet ditt, bytt ut `ADRESSE` (og
   `BRUKER` på Mac), og start den:
   - Mac: `deploy/launchd/spond-familie-http.plist` → `~/Library/LaunchAgents/`,
     så `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/spond-familie-http.plist`.
   - Linux/Pi: `deploy/systemd/spond-familie-http.service` (instruksjoner i fila).

   Malene starter i **lesemodus** (`SPOND_MCP_READONLY=1`). Fjern den linjen når
   du har sjekket at lesing virker.
5. **Slå på Funnel** til tjenesten:
   ```bash
   tailscale funnel --bg --https=443 http://127.0.0.1:8765
   ```
   Første gang ber Tailscale deg slå på Funnel i admin-konsollet.
6. **Sjekk utenfra** (f.eks. fra mobilnettet). Svaret skal være `401`:
   ```bash
   curl -i https://ADRESSE/mcp
   ```
7. **Legg til i Claude:** claude.ai → Innstillinger → Connectors → **Add custom
   connector** → `https://ADRESSE/mcp` → logg inn med passfrasen.
   Sett `rsvp` og `send_message` til **Needs approval** under Tool permissions.
   Connectoren dukker også opp i Claude-appen på mobilen.

### Fallgruver vi har gått i

- **Port 443 er opptatt på maskinen?** Funnel kan bare bruke én tjeneste per
  port. Lag i stedet en egen Tailscale-node for spond-familie, med et eget
  vertsnavn og egen 443. Bruk `tailscaled` i brukermodus, uten root:
  ```bash
  # Mac: brew install tailscale && brew unlink tailscale   (unlink: `tailscale` er fortsatt appens)
  T=$(brew --prefix)/opt/tailscale/bin      # Linux: T=/usr/bin
  D=$HOME/.local/state/spond-familie/tailscale && mkdir -p -m 700 $D
  $T/tailscaled --tun=userspace-networking --statedir=$D --socket=$D/tailscaled.sock --port=0 &
  $T/tailscale --socket=$D/tailscaled.sock up --hostname=spond
  $T/tailscale --socket=$D/tailscaled.sock funnel --bg --https=443 http://127.0.0.1:8765
  ```
  Adressen blir `https://spond.<tailnet>.ts.net`. Kjør `tailscaled` som tjeneste
  (launchd/systemd) så den starter av seg selv.
- **Nytt vertsnavn får ikke offentlig DNS med en gang.** I oktober 2026 tok det
  ca. 30 minutter
  ([tailscale#21502](https://github.com/tailscale/tailscale/issues/21502)). Sjekk
  med `dig +short @8.8.8.8 ADRESSE`.
- **Bytter du modus** (lesing/skriving), må du koble connectoren fra og til, eller
  starte en ny samtale, før Claude ser de nye verktøyene.

## Rask test uten Tailscale: Cloudflare-hurtigtunnel

For å prøve med en gang, uten konto. Adressen er tilfeldig og endres ved hver
omstart, så dette egner seg bare til testing:

```bash
brew install cloudflared            # eller se cloudflare.com
cloudflared tunnel --url http://127.0.0.1:8765
# → https://<tilfeldige-ord>.trycloudflare.com
spond-familie serve-http --public-url https://<tilfeldige-ord>.trycloudflare.com
```

## Drift

```bash
spond-familie status        # Spond-innloggingen (fornyes automatisk ved bruk)
spond-familie revoke-all    # logg ut alle Claude-tilkoblinger (start tjenesten på nytt etterpå)
```

- Tjenesten bruker bare tokenet fra `spond-familie login`. Den leser aldri
  passordet, og nekter å starte hvis `SPOND_PASSWORD` er satt.
- Den sjekker Spond-innloggingen hver 6. time og fornyer den når det trengs, så
  den ikke går ut så lenge tjenesten kjører.
- Loggen viser metode, sti og status, aldri tokener.
