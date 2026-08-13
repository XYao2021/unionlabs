#!/usr/bin/env python3
"""Preprocess SYSTEM_REFERENCE.md so every glyph renders under xelatex with
Charter (body) / Menlo (mono). Converts Unicode super/subscript runs to ^/_
notation and swaps symbols the fonts lack for safe equivalents."""
import re, sys, unicodedata

src, dst = sys.argv[1], sys.argv[2]
t = open(src, encoding="utf-8").read()
t = unicodedata.normalize("NFC", t)      # compose s+combining-hat into one codepoint

SUP = {'⁰':'0','¹':'1','²':'2','³':'3','⁴':'4',
       '⁵':'5','⁶':'6','⁷':'7','⁸':'8','⁹':'9',
       '⁺':'+','⁻':'-','ⁿ':'n'}
SUB = {'₀':'0','₁':'1','₂':'2','₃':'3','₄':'4',
       '₅':'5','₆':'6','₇':'7','₈':'8','₉':'9'}

def group(chars, prefix):
    cls = ''.join(re.escape(c) for c in chars)
    def repl(m):
        s = ''.join(chars[c] for c in m.group(0))
        return prefix + (s if len(s) == 1 else '(' + s + ')')
    return lambda text: re.sub('[' + cls + ']+', repl, text)

t = group(SUP, '^')(t)
t = group(SUB, '_')(t)
t = t.replace('⌊', 'floor(').replace('⌋', ')')

MISC = {'ᴴ':'^H', '≲':'<~', '≳':'>~', '∈':' in ', '∉':' not in ',
        '‖':'||', '►':'>', '▸':'>',
        '→':'->', '←':'<-', '↔':'<->', '⇒':'=>', '⇄':'<->', '☐':'[ ]'}
for k, v in MISC.items():
    t = t.replace(k, v)

open(dst, 'w', encoding='utf-8').write(t)
safe = set('·−×÷±≈≤≥√ΔΣΠαβγδεζηθλμνπρστφωΩ°—–’…“”§½üŝ')
rest = sorted({c for c in t if ord(c) > 0x7f and c not in safe})
print("remaining non-ASCII (should be Charter/Menlo-safe):", ' '.join(rest) or "(none)")
