# Krateo Akmon — name and marks

## The name

**Akmon** — Greek *ἄκμων*, **anvil**.

It is the sibling root to Krateo's own name: Krateo is *κρατέω*, "to hold, to
have power over". Akmon is the thing you hold the metal against. The two words
come from the same language and the same register, which is why they sit
together without explanation.

The metaphor is also exact, in a way that matters for what this product
formally *is*. **An anvil shapes the metal without becoming part of it.** CNCF
distinguishes an *installer*, which "downloads and then installs vanilla
upstream Kubernetes", from a *distribution*, which "includes patches to the
upstream codebase". This is an installer: it provisions machines and installs
unmodified upstream Kubernetes from `pkgs.k8s.io`, and patches nothing. The
anvil leaves no trace in the blade.

Two syllables, five letters, one obvious spelling, and it reads the same in
Italian and English.

### Names that were rejected, and why

Worth recording so nobody re-proposes them.

- **Tempra** (Italian, *tempering*). The metaphor was good and the word has a
  nice second meaning — *mettle*. Rejected because the **Fiat Tempra** was a
  poorly-regarded 1990s saloon, and for an Italian company the association is
  immediate and unflattering. The lesson: a dead trademark is not a free name.
  Connotation in the home market matters more than whether anyone can sue.
- **Fucina** (Italian, *forge*). Positive in Italian — *una fucina di talenti*
  is a compliment — but the first three letters read badly to English speakers.
- **Lingotto** (*ingot*). Also Fiat: the Lingotto factory in Turin.
- **Lega** (*alloy*). In Italy the word is first and foremost a political
  party.
- **Forge**. Already taken in this exact niche — **Cloud&Heat Forge** is a
  bare-metal Kubernetes distribution.
- **Sideros** (Greek, *iron*). Taken in this exact niche too: **Sidero Metal**
  from Sidero Labs is a bare-metal Kubernetes provisioner, and the name comes
  from the same root.
- **Incudine** (Italian, *anvil*). Clear and unclaimed, but four syllables
  where Akmon is two.

Akmon was checked the same way and found unclaimed in the cloud-native and
Kubernetes space.

Full product name: **Krateo Akmon**.

## The mark

A Greek capital **alpha** whose crossbar is an **anvil**.

One shape, two readings. The Α is for Akmon and echoes the Greek root the name
comes from. The band through it is the face and body of an anvil — flat top,
waisted below — and it carries the temper colours of worked steel, straw into
bronze. The band overhangs the letter on both sides, the way an anvil's face
overhangs its waist.

It is two paths and one gradient. There is deliberately **no Kubernetes wheel
and no CNCF artwork** in it: the Kubernetes mark is trademarked and may not be
altered or absorbed into another logo.

### What the exploration ruled out

Four directions were drawn and compared side by side before this one was
chosen.

- **A literal anvil silhouette.** Competent, but the horn reads as a pencil
  tip at the sizes a logo is actually used at.
- **Six cells in a 3 + 3 grid** (three tempered above three in steel, the
  reference topology). It encoded something true, but it looks like a generic
  application-grid icon — it could belong to any product.
- **A negative-space anvil between two server racks.** The anvil in the gap
  was too small to register and read as a fire hydrant.
- **Adding a horn to the chosen band.** Tried twice. It turns the band into a
  left-pointing arrow and makes the anvil reading *weaker*, not stronger. The
  symmetric band won.

The chosen mark was then checked at 32px and 20px. It holds at 32; at 20 the
waist starts to close up, which is the normal floor for a two-element mark and
the reason the wordmark lockups exist.

## Files

| File | Use |
|---|---|
| `krateo-akmon-logo.svg` | Primary lockup, full colour, light backgrounds |
| `krateo-akmon-logo-reverse.svg` | White lockup, dark backgrounds |
| `krateo-akmon-logo-mono.svg` | Single dark ink, one-colour printing |
| `krateo-akmon-mark.svg` | The mark alone, square, for avatars and favicons |

All are SVG. **This is a requirement, not a preference**: CNCF accepts
`product_logo_url` only in SVG, AI or EPS — explicitly *not* PNG — and requires
the logo to contain the product name. The three lockups carry the wordmark; the
bare mark does not, so **do not submit `krateo-akmon-mark.svg` to CNCF.** Use
`krateo-akmon-logo.svg`.

`product_logo_url` is optional; omitted, CNCF uses the company logo.

## Colours

| Token | Hex | Where |
|---|---|---|
| Temper straw | `#F0C04E` | gradient start, control plane |
| Temper bronze | `#C9733A` | gradient middle |
| Temper purple | `#8A4A6B` | gradient end |
| Steel | `#3B5BA5` | worker cells |
| Ink | `#1B2430` | wordmark |
| Ink muted | `#5A6676` | tagline |

## Using them

The wordmark uses a Helvetica/Arial stack rather than a bundled font, so it
renders with no external dependency — but the exact glyphs then follow whatever
is installed. **Before sending the logo anywhere that matters, convert the text
to paths**, or a renderer elsewhere may substitute a font and change the
spacing.

The mono and reverse files paint everything with `currentColor`, so dropping
either into HTML and setting `color` on a parent recolours the whole logo. The
consequence is worth knowing: rendered standalone, with no parent to inherit
from, each falls back to its own default — dark ink for mono, white for
reverse. Pick the file that matches the background rather than expecting one
file to adapt. (This was found by rendering the mono file on a dark background
and getting an almost invisible logo.)

Minimum width for the full lockup is about 180px; below that the tagline stops
being legible and the bare mark is the better choice.
