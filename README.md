# jhu_software_concepts

Coursework for JHU EN.605.256 Modern Software Concepts in Python.

## Module 4 - Pytest and Sphinx

* Code, tests and README: [`module_4/`](module_4/README.md)
* Documentation (Sphinx, on Read the Docs): https://sassom2112-jhu-software-concept.readthedocs.io/en/latest/
* CI: [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs the Module 4 test suite against PostgreSQL on every push that changes `module_4/`

## Module 5 - Software Assurance

* Code, tests and README: [`module_5/`](module_5/README.md)
* CI: [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs four jobs (Pylint 10/10, the pydeps dependency graph, Snyk, and the test suite with 100% coverage) on every push that changes `module_5/`

## Module 6 - Deploy Anywhere

* Code, tests and README: [`module_6/`](module_6/README.md); report with screenshots: [`module_6/module_6_report.pdf`](module_6/module_6_report.pdf)
* Docker Hub (public): https://hub.docker.com/r/frametotoro/module_6 (tags `web-v1` and `worker-v1`)
* CI: [`.github/workflows/module_6.yml`](.github/workflows/module_6.yml) runs three jobs (Pylint 10/10, the test suite with 100% coverage, and a Docker build of both images) on every push that changes `module_6/`

## Module 7 - Cloud Computing

* Code, notebook and README: [`module_7/`](module_7/README.md): the SageMaker notebook `grad-cafe-pipeline.ipynb` downloads the Grad Café data from S3 with boto3 (`s3_fetch.py`) and saves it as `applicant_data_SM.json`
* EC2 deployment of the Module 6 stack: [`module_7/ec2/EC2_DEPLOYMENT.md`](module_7/ec2/EC2_DEPLOYMENT.md) and [`module_7/ec2/docker-compose.ec2.yml`](module_7/ec2/docker-compose.ec2.yml)
