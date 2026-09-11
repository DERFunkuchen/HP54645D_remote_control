# Manuals

This package was written from the **HP 54645A/D Programmer's Guide**,
publication 54645-97000, first edition June 1996. Section references (§4-8,
§8-15, …) throughout the code and docs are to that document: chapter 4 for
RS-232, chapter 8 for the full command list.

The guide is HP's copyright and is not included in this repository. It is
widely mirrored; one copy is at
[docs.ampnuts.ru](https://docs.ampnuts.ru/eevblog.docs/HP_Agilent_Keysight/HP%2054645A,%2054645D%20Programmer.pdf).
Keysight, which owns the product line today, may also have it in its support
archive. If you download it, you can keep it here — `*.pdf` in this folder is
ignored by git.

The guide refers to a *Programmer's Reference* on a 3.5" diskette for the
per-command detail; that is not widely available. Chapter 8's quick reference
is what this package was written from, and where it and chapter 2 disagree
(the timebase mode is `MAIN` in §8, `NORMal` in §2-6), the instrument sided
with §8.
