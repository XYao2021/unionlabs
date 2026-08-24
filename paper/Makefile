# make        -> mobicom-demo.pdf
# make clean  -> remove build artifacts
# make pages  -> print the page count (body must be 2, references start on 3)
LATEXMK ?= latexmk
PAPER   := mobicom-demo

$(PAPER).pdf: $(PAPER).tex bib.bib
	$(LATEXMK) -pdf -halt-on-error $(PAPER).tex

pages: $(PAPER).pdf
	@pdfinfo $(PAPER).pdf 2>/dev/null | grep Pages || \
	 python3 -c "import re,sys; d=open('$(PAPER).pdf','rb').read(); \
	 print('Pages:', max(int(m) for m in re.findall(rb'/Count\s+(\d+)', d) or [b'0']))"

clean:
	$(LATEXMK) -C $(PAPER).tex 2>/dev/null || true
	rm -f $(PAPER).bbl $(PAPER).blg

.PHONY: pages clean

slides: unionlabs-slides.pdf
unionlabs-slides.pdf: unionlabs-slides.tex
	pdflatex -interaction=batchmode unionlabs-slides.tex
	pdflatex -interaction=batchmode unionlabs-slides.tex
	@rm -f unionlabs-slides.aux unionlabs-slides.log unionlabs-slides.nav \
	       unionlabs-slides.out unionlabs-slides.snm unionlabs-slides.toc

.PHONY: slides
