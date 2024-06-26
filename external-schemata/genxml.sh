#!/bin/bash

pip install 'ruff>=0.3.0'

xsdata generate \
     --config ./external-schemata/.xsdata.xml \
          -p pyhanko.generated \
     --recursive \
     ./external-schemata/xsd/

isort --profile black --line-length 80 pyhanko/generated
black -S --line-length 80 pyhanko/generated
