# make        -> all three figure PDFs
# make png    -> quick-look PNGs at 130 dpi
FIGS := fig-union-api fig-phy-pipeline fig-shared-workspace \
        fig-union-api-col fig-phy-pipeline-col fig-shared-workspace-col \
        fig-agent-col
PDFS := $(FIGS:=.pdf)

all: $(PDFS)

%.pdf: %.tex unionstyle.tex
	pdflatex -interaction=batchmode $<
	@rm -f $*.aux $*.log

png-300: $(PDFS)
	for f in fig-union-api-col fig-agent-col fig-shared-workspace-col fig-phy-pipeline; do \
	  pdftoppm -png -r 300 $$f.pdf $$f-300; done

png: $(PDFS)
	for f in $(FIGS); do pdftoppm -png -r 130 $$f.pdf $$f; done

clean:
	rm -f *.aux *.log *.png $(PDFS)

.PHONY: all png png-300 clean
