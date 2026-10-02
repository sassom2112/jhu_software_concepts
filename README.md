# jhu_software_concepts

Coursework for JHU EN.605.256 Modern Software Concepts in Python.

## Module 4 - Pytest and Sphinx

* Code, tests and README: [`module_4/`](module_4/README.md)
* Documentation (Sphinx, on Read the Docs): https://sassom2112-jhu-software-concept.readthedocs.io/en/latest/
* CI: [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs the Module 4 test suite against PostgreSQL on every push that changes `module_4/`

## Module 5 - Software Assurance

* Code, tests and README: [`module_5/`](module_5/README.md)
* CI: [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs four jobs (Pylint 10/10, the pydeps dependency graph, Snyk, and the test suite with 100% coverage) on every push that changes `module_5/`
