export PYTHONPATH := src

demo:            ## generate data, reconcile, verify -- start here
	python3 -m reconciler demo

generate:        ## rebuild the deterministic test dataset
	python3 -m reconciler generate

reconcile:       ## reconcile and print the console report
	python3 -m reconciler reconcile

verify:          ## prove every injected defect is detected
	python3 -m reconciler verify

report:          ## write the markdown report to out/
	python3 -m reconciler reconcile --format markdown --out out/reconciliation-report.md

processors:      ## show registered processors, currencies and detectors
	python3 -m reconciler processors

test:            ## run the test suite
	python3 -m unittest discover tests -v

.PHONY: demo generate reconcile verify report processors test
