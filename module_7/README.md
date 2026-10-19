# Module 7 - Cloud Computing: S3 to SageMaker with boto3, and the Module 6 Stack on EC2

**Name:** Mike Sasso (JHED: msasso1)
**Course:** JHU EN.605.256 Modern Software Concepts in Python
**Module:** Module 7 - Cloud Computing (IAM, S3, SageMaker, boto3, EC2, Docker Compose)

| | |
| --- | --- |
| AWS setup | Account, root MFA, the IAM user `dailyWork-ms` and its policies, the bucket and the notebook instance, with the four Part 1 screenshots (section 2) |
| Notebook | [`grad-cafe-pipeline.ipynb`](grad-cafe-pipeline.ipynb): downloads `applicant_data.json` from S3 and saves it as `applicant_data_SM.json` (section 3) |
| boto3 code | [`s3_fetch.py`](s3_fetch.py), imported by the notebook; also a small command-line tool (section 4) |
| Output | `applicant_data_SM.json`, in the same folder as the notebook (on SageMaker: `/home/ec2-user/SageMaker/`) |
| EC2 deployment | [`ec2/EC2_DEPLOYMENT.md`](ec2/EC2_DEPLOYMENT.md) (steps and troubleshooting), [`ec2/docker-compose.ec2.yml`](ec2/docker-compose.ec2.yml), [`ec2/.env.example`](ec2/.env.example) |
| Pylint 10.00/10 | `pylint *.py tests` (or `pylint --ignore=.venv .`) and `nbqa pylint grad-cafe-pipeline.ipynb`, in `module_7/` (default settings, section 7) |
| Tests | 46 offline tests: `python -m pytest` in `module_7/` (no AWS account or network needed) |
| Resources stopped | section 9 |
| Repository | https://github.com/sassom2112/jhu_software_concepts/tree/master/module_7 |

Part 1 moves the Grad Café data into a compute workspace: the file sits in a private S3 bucket, and a
SageMaker notebook pulls it down with boto3. Part 2 runs the Module 6 system (Flask web, worker,
PostgreSQL, RabbitMQ) on an EC2 instance with Docker Compose, using the images already published on
Docker Hub.

---

## 1. What is in this folder

| File | What it is |
| --- | --- |
| `grad-cafe-pipeline.ipynb` | The SageMaker notebook. It imports `s3_fetch`, downloads the data and prints a short report. |
| `s3_fetch.py` | All boto3 code: session, download, checks, error messages, command line. The bucket's name is set here, once (`DEFAULT_BUCKET`). |
| `requirements.txt` | Exact versions of boto3 and the lint and test tools. |
| `pytest.ini`, `tests/` | Offline tests: `s3_fetch.py` against moto's fake S3, the notebook's cells run end to end, the EC2 compose rules, and a scan for keys. |
| `ec2/docker-compose.ec2.yml` | The Module 6 stack for one EC2 instance (published images only, nothing built). |
| `ec2/.env.example` | Every setting the compose file reads, with `change-me` placeholders. |
| `ec2/EC2_DEPLOYMENT.md` | The deployment steps, the measured memory use, and troubleshooting notes. |
| `mfa.png` | Root user's MFA device, active. |
| `dailyWork.png` | Signed in as `dailyWork-ms`: the permissions page and the account menu at the top right. |
| `grad-cafe-bucket.png` | The bucket's contents, showing `applicant_data.json`. |
| `liveNotebook.png` | The notebook instance `s3-to-sagemaker-grad-cafe-pipeline` InService, with the user name at the top right. |
| `ec2-instance.png` | The EC2 instance running, with its public IPv4 address. |
| `ec2-security-group.png` | The inbound rules: 22 and 8080 from my IP only. |
| `ec2-compose-ps.png` | `docker compose ps -a` on the instance: all services running or healthy, `init` exited 0. |
| `ec2-app.png` | The analysis page in the browser at `http://54.187.45.90:8080`, the instance's public IPv4 on 2026-10-18. |
| `ec2-worker-log.png` | The worker log on the instance after Update Analysis and Pull Data: both tasks committed and acknowledged. |

## 2. AWS setup (console steps)

These are the console steps in the assignment's order, as AWS's documentation describes the console in
October 2026. A label marked † is what the console showed but the documentation does not spell out;
check it on screen. Use one Region for everything (top right of the console; this account uses
US West (Oregon), `us-west-2`): the bucket, the notebook instance and the EC2 instance. On a global
service such as IAM the Region menu reads **Global** and every Region shows a lock; it unlocks on a
regional service such as S3.

### 2.1 The account

1. Sign up at https://signin.aws.amazon.com/signup?request_type=register: root email address and
   account name, **Verify email address**, the emailed code, **Verify**, root password, the account
   plan, contact details, a credit card, phone verification, the free **Basic support** plan,
   **Complete sign up**.
2. Use this email-and-password sign-up. If AWS offers **Sign up for AWS (new)** (an AWS Builder ID or a
   Google, GitHub or Apple login), do not use it: such an account has no root user, and its IAM users
   cannot sign in to the console, so neither `mfa.png` nor `dailyWork.png` could be taken.
3. Plan: the Free plan and the Paid plan both start with $100 of credits, plus up to $100 more for
   five starter activities (one of them is the budget in 2.2). The Free plan never charges the card;
   it closes after 6 months or when the credits run out, whichever comes first (Module 8 is inside that
   window), and it can be upgraded to Paid at any time, keeping the credits. EC2, S3 and SageMaker AI
   are offered on both plans.

Cost, from the October 2026 prices (the same in us-west-2 and us-east-1): the `ml.t3.medium` notebook
instance costs $0.05 an hour while it runs, and its 5 GB volume $0.70 a month; the `t3.micro` EC2
instance $0.0104 an hour, its public IPv4 address $0.005 an hour, its 16 GiB gp3 volume $1.28 a month;
36 MB in S3 about $0.001 a month. A few hours of each come to about $2-3 for the month, which the
credits cover.

### 2.2 Root user: MFA (`mfa.png`) and a budget

Signed in as root:

1. Account name (top right) > **Security credentials** > **Multi-factor authentication (MFA)** >
   **Assign MFA device**.
2. **Device name** (for example `root-phone`), **Authenticator app**, **Next**. **Show QR code** and
   scan it with the app; type the code it shows as **MFA code 1**, wait for the next code and type it as
   **MFA code 2**, then **Add MFA** straight away. (AWS requires root MFA anyway, within 35 days of the
   first console sign-in.)
3. `mfa.png`, still signed in as root: the IAM console's **Dashboard**, whose **Security
   recommendations** panel shows a green check next to **Root user has MFA**†. If the panel looks
   different, the **Multi-factor authentication (MFA)** section of **Security credentials**, listing the
   device, shows the same thing.
4. Optional, and it earns a $20 credit: **Billing and Cost Management > Budgets > Create budget > Use
   a template (simplified) > Monthly cost budget**, amount `5`, your email address, **Create budget**.
   Budgets subtract credits by default, so the alert may not fire while credits last.
5. Never create access keys for the root user.

### 2.3 The IAM user `dailyWork-ms` (`dailyWork.png`)

Still as root, IAM console > **Users** > **Create user**:

1. **User name** `dailyWork-ms`. Tick **Provide user access to the AWS Management Console**, choose
   **I want to create an IAM user**, and set a **Console password**. Ticking **User must create a new
   password at next sign-in** is fine; the console then adds `IAMUserChangePassword`† (table below).
2. **Set permissions** > **Attach policies directly**, or **Add user to group** with a group that holds
   the same policies (this account uses the group `dailywork-jh`, so the **Permissions** tab lists
   each policy as "Attached via Group"). Tick these AWS managed policies, then **Next** and **Create
   user**:

   | Policy | What `dailyWork-ms` needs it for |
   | --- | --- |
   | `AmazonS3FullAccess` | creating the bucket with Block Public Access on, uploading the file, viewing the contents |
   | `AmazonSageMakerFullAccess` | creating, opening and stopping the notebook instance, and passing its execution role to SageMaker (`iam:PassRole`) |
   | `AmazonEC2FullAccess` | launching and stopping the instance, its key pair and its security group (Part 2) |
   | `IAMFullAccess` | the notebook form's **Create a new role**, which needs `iam:CreateRole`, `iam:CreatePolicy` and `iam:AttachRolePolicy` (`AmazonSageMakerFullAccess` has only `iam:PassRole` and `iam:ListRoles`); creating the user's own access key and MFA device; opening its own **Permissions** tab for `dailyWork.png` |
   | `IAMUserChangePassword` | added by the console when "must create a new password" is ticked |

   With only the first three, three later steps fail: creating the notebook's role, `dailyWork.png`,
   and creating the access key. `IAMFullAccess` is broad (section 8 has the trade-off and a narrower
   alternative).
3. On the last page, **Download .csv file** (sign-in URL, user name, password). Keep it outside the
   repository and delete it once the password is stored safely.
4. Sign out of root. Sign in as `dailyWork-ms` at `https://<ACCOUNT_ID>.signin.aws.amazon.com/console/`
   (the URL in the CSV). Everything below is done as `dailyWork-ms`.
5. `dailyWork.png`: IAM > **Users** > `dailyWork-ms` > **Permissions** tab, listing the policies, with
   the account menu at the top right (it shows `dailyWork-ms`) in the picture.
6. Recommended: MFA for this user as well. Top right > **Security credentials** > **Assign MFA device**,
   the same steps as 2.2.

### 2.4 The S3 bucket (`grad-cafe-bucket.png`)

S3 console > **General purpose buckets** > **Create bucket**:

1. **Bucket name**: `grad-cafe`, or `grad-cafe-ms` if that is taken. Bucket names are shared by every
   AWS account, and both were already taken on 2026-10-17: an anonymous request to
   `https://<name>.s3.amazonaws.com/` answered 403 for both, which means the bucket exists (in another
   account, unless it is yours), while a free name answers 404. If S3 refuses both, add a short
   suffix to the initials version. This account's bucket is **`grad-cafe-msasso1`** (`grad-cafe-` plus
   my JHED ID, `msasso1`, which starts with the initials), created in `us-west-2` on 2026-10-18.
2. Keep the global bucket namespace. The console's "account regional namespace" option would add
   `-<account-id>-<region>-an` to the name.
3. **Block all public access**: leave it on (the default). Object Ownership and encryption: the
   defaults.
4. **Create bucket**. Open the bucket, **Upload > Add files**, choose `applicant_data.json` from
   `module_6/src/data/` (36,355,821 bytes), **Upload**. To reach that folder from the Windows file
   dialog, run `cd module_6/src/data && explorer.exe .` in WSL at the top of the repository.
   Use **Add files**, not **Add folder**, and do not drag the `data` folder in: a folder upload puts
   the file under `data/` (the upload list's **Folder** column reads `data/` instead of `-`), and the
   object's key becomes `data/applicant_data.json`, which `s3_fetch.py` does not look for.
5. `grad-cafe-bucket.png`: the bucket's **Objects** tab listing `applicant_data.json` at the top
   level of the bucket.
6. The bucket's name is set once, as `DEFAULT_BUCKET` near the top of `s3_fetch.py`
   (`"grad-cafe-msasso1"`). The notebook, the command line and the tests all read it from there.

### 2.5 The SageMaker notebook instance (`liveNotebook.png`)

1. Open https://console.aws.amazon.com/sagemaker/, the **Amazon SageMaker AI** console (the console's
   search may offer "Amazon SageMaker" Unified Studio first; that is a different product), in the
   bucket's Region. In the left menu, **Applications and IDEs > Notebooks** > **Create notebook
   instance**.
2. Fill in the form:

   | Field | Value |
   | --- | --- |
   | Notebook instance name | `s3-to-sagemaker-grad-cafe-pipeline` (it cannot be renamed later) |
   | Notebook instance type | `ml.t3.medium` (`ml.t2.medium`, the assignment's choice, can no longer be picked, see below) |
   | Platform identifier | `notebook-al2023-v1` (Amazon Linux 2023, JupyterLab 4), the default. The assignment's screenshot shows Amazon Linux 2 with JupyterLab 3; Amazon Linux 2 notebook instances cannot be created or restarted since July 1, 2026. |
   | Permissions and encryption > IAM role | **Create a new role**; under S3 buckets you specify, **Specific S3 buckets**† = the bucket's name; **Create role** |
   | Everything else | the defaults (root access enabled, no VPC, no Git repository, 5 GB volume) |

   On 2026-10-18 the Notebooks page showed "The t2 instance family is being deprecated for SageMaker
   notebook instances. New notebook instances can no longer be created with ml.t2 instance types"
   (the banner is in `liveNotebook.png`). `ml.t3.medium` is the replacement AWS recommends and the
   cheapest notebook type left: 2 vCPUs, 4 GiB, $0.05 an hour in `us-west-2` ($0.0464 for
   `ml.t2.medium`).

   If the role dialog has no **Specific S3 buckets** choice, make the role in IAM instead: **Roles >
   Create role > AWS service > SageMaker AI > SageMaker AI - Execution**, **Next** to the end, **Create
   role**. On the new role, **Add permissions > Create inline policy > JSON**, paste this with the
   bucket's name, and save it. Then pick that role in the notebook form (**Use existing role**†).

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<BUCKET>"},
       {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::<BUCKET>/*"}
     ]
   }
   ```

3. **Create notebook instance**. Its status goes from **Pending** to **InService** in a few minutes
   (the assignment creates the access key, 2.6, while it waits).
4. `liveNotebook.png`: the **Notebooks** list with the instance **InService**, with the account menu at
   the top right (`dailyWork-ms`) in the picture.

### 2.6 The access key (only for running `s3_fetch.py` on a laptop)

The notebook never uses it: on SageMaker, boto3 gets temporary credentials from the execution role.
The assignment asks for one, and it lets `s3_fetch.py` run from WSL (section 4). None was created for
this submission (`dailyWork.png` shows **Create access key** under Access key 1), because the notebook
does not need one. To create one:

1. As `dailyWork-ms`: top right > **Security credentials** > **Access keys** > **Create access key**.
   Use case **Command Line Interface (CLI)**, tick the confirmation†, **Next**, **Create access key**,
   **Download .csv file**. The secret key is shown only this once.
2. The browser saves the CSV in Windows' Downloads folder. Move it to the Linux side (`~`), never into
   the repository (`*accessKeys.csv` is git-ignored), load it with `aws configure --profile
   dailywork` (section 4), then delete the CSV.
3. When it is no longer needed: **Access keys > Actions > Deactivate**, then **Delete**.

## 3. Part 1: run the notebook in SageMaker

### What section 2 set up

| Resource | Setting |
| --- | --- |
| IAM user | `dailyWork-ms`, used for all the work below (the root user is only for account settings) |
| S3 bucket | `grad-cafe-msasso1` in `us-west-2`, **Block all public access: on** (`grad-cafe` and `grad-cafe-ms` already existed in other accounts, section 2.4) |
| Object | `applicant_data.json` from `module_6/src/data/` (36,355,821 bytes, 30,500 records) |
| Notebook instance | `s3-to-sagemaker-grad-cafe-pipeline`, type `ml.t3.medium` (no new `ml.t2` notebooks, section 2.5), InService on 2026-10-18 |
| Platform | `notebook-al2023-v1` (JupyterLab 4), the default |
| IAM role | the notebook's execution role, created in the notebook form with **Specific S3 buckets** = the bucket name |

The execution role is how the notebook gets permission to read the bucket. Nothing in the notebook
holds a key.

### Steps

1. Check that `DEFAULT_BUCKET` in `s3_fetch.py` is the bucket's name (2.4, step 6).
2. In the SageMaker console, open **Notebooks**, select `s3-to-sagemaker-grad-cafe-pipeline` and
   click **Open JupyterLab** once its status is **InService**.
3. In JupyterLab's file browser, click **Upload Files** and upload two files from this folder:
   `grad-cafe-pipeline.ipynb` and `s3_fetch.py`. They must be in the same folder, because the notebook
   imports `s3_fetch` from its own folder.
4. Open `grad-cafe-pipeline.ipynb`. If JupyterLab asks for a kernel, choose **conda_python3**
   (Python 3.10, boto3 included). Nothing needs to be installed, and no cell needs editing.
5. Run all cells (**Run > Run All Cells**). The run on 2026-10-18 (conda_python3, Python 3.10.20)
   printed this in the download cell:

   ```text
   Downloaded s3://grad-cafe-msasso1/applicant_data.json
   Saved as   /home/ec2-user/SageMaker/applicant_data_SM.json
   Size       36,355,821 bytes
   Records    30,500
   Credentials came from boto3's default chain: iam-role
   ```

   `iam-role` is botocore's name for the credential source: temporary credentials from the notebook
   instance's execution role, not an access key. The last cell reads the saved file back: 30,500
   records with the same 21 fields as `module_6/src/data/applicant_data.json`, and the same counts by
   status (Rejected 14,208, Accepted 11,371, Waitlisted 2,735, Interview 2,186).
6. Save the notebook (**File > Save**), then right-click it in the file browser, choose **Download**,
   and replace `module_7/grad-cafe-pipeline.ipynb` with the downloaded copy, so the committed notebook
   shows the outputs of the SageMaker run. Run `nbqa pylint grad-cafe-pipeline.ipynb` and
   `python -m pytest` again afterwards (section 7); the key scan also reads the saved outputs.

### Where the output is written

`applicant_data_SM.json` is written to the notebook's own folder, which is the working directory of a
Jupyter kernel. If the two files were uploaded to the top of the file browser, that is
`/home/ec2-user/SageMaker/applicant_data_SM.json`. That folder is on the notebook instance's storage
volume, so the file is still there after the instance is stopped and started again. The file is not
committed (it is 36 MB and listed in the repository's `.gitignore`).

## 4. How the S3 download works

`fetch_applicant_data(bucket, key, output)` in `s3_fetch.py` does five things:

1. **Session.** `boto3.session.Session()` with no keys passed. boto3's default credential chain looks
   for credentials in this order and uses the first it finds:

   | Where boto3 looks | Used when |
   | --- | --- |
   | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables | set in the shell |
   | `~/.aws/credentials` and `~/.aws/config` (profile from `AWS_PROFILE` or `--profile`) | on a laptop, after `aws configure` |
   | the IAM role of the machine | on the SageMaker notebook instance (its execution role) |

   The report prints which source was used, never the keys.
2. **Check the object.** `head_object` returns the object's size. A wrong bucket or key fails here with a
   short explanation, for example
   `s3://grad-cafe-msasso1/applicant_data.json: the bucket or the object does not exist (check the bucket name and the key) (S3 error code 404)`,
   which is what a folder upload (key `data/applicant_data.json`, 2.4) would give. A 403 means the
   role or user may not read the object, or the bucket belongs to another account (as `grad-cafe`
   and `grad-cafe-ms` do).
3. **Download.** `download_file` writes to `applicant_data_SM.json.part` first.
4. **Check the file.** Its size must equal the size S3 reported, and it must be a JSON list of records.
5. **Save.** Only then is the `.part` file renamed to `applicant_data_SM.json`. If any step fails, the
   `.part` file is deleted and an older `applicant_data_SM.json` stays as it was.

The bucket is `DEFAULT_BUCKET` in `s3_fetch.py`. The `GRAD_CAFE_BUCKET` environment variable, or
`--bucket` on the command line, overrides it for one run without editing any code.

### Running it on a laptop (optional)

The same code runs outside SageMaker with an access key for `dailyWork-ms` (2.6). The AWS CLI
([install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)) stores
the key in `~/.aws/credentials`, outside the repository. Run the script with the virtual environment
from section 5 active (`source .venv/bin/activate` in `module_7/`):

```bash
aws configure --profile dailywork           # paste the key ID and secret from the downloaded CSV
cd module_7
python s3_fetch.py --profile dailywork      # writes ./applicant_data_SM.json
python s3_fetch.py --help
```

## 5. Dependencies

* **In SageMaker:** none to install. The `conda_python3` kernel already has boto3, and the notebook
  imports only boto3 (through `s3_fetch`) and the standard library.
* **On a laptop:** Python 3.10 or newer (this was developed with Python 3.11.13), then

  ```bash
  cd module_7
  python3 -m venv .venv      # or a specific one, e.g. ~/.pyenv/versions/3.11.13/bin/python -m venv .venv
  source .venv/bin/activate  # from here on, `python` is the environment's Python
  pip install -r requirements.txt
  ```

`requirements.txt` pins every package exactly, in three groups: boto3 and what it needs (botocore,
s3transfer, jmespath, python-dateutil, six, urllib3); pylint and nbqa for linting; pytest and moto for
the tests. The lines with a `python_version` marker differ between Python 3.10 and 3.11+: ipython and
platformdirs dropped 3.10 in their newest versions, and the other marked lines are dependencies that
only one side needs.

## 6. Part 2: the Module 6 stack on EC2

[`ec2/EC2_DEPLOYMENT.md`](ec2/EC2_DEPLOYMENT.md) has every step: launching the instance, the security
group, SSH from WSL, installing Docker and Compose, copying the three files with `scp`, starting the
stack, checking both buttons and the worker log, the SSH tunnel to the RabbitMQ UI, and stopping the
instance. In short:

```bash
ssh -i ~/.ssh/<KEY_NAME>.pem ubuntu@<EC2_PUBLIC_IPV4>
cd ~/gradcafe        # docker-compose.ec2.yml, .env, data/applicant_data.json
docker compose -f docker-compose.ec2.yml --env-file .env up -d
docker compose -f docker-compose.ec2.yml ps -a
```

| Port on the instance | Service | Who can reach it |
| --- | --- | --- |
| 22 | SSH | my IP only (security group) |
| 8080 | the Flask page, `http://<EC2_PUBLIC_IPV4>:8080` | my IP only (security group) |
| 15672 | RabbitMQ management UI | the instance itself (`127.0.0.1`), so only through an SSH tunnel |
| 5432, 5672 | PostgreSQL, AMQP | not published; only the containers reach them |

The web and worker containers use the Module 6 images `frametotoro/module_6:web-v1` and
`frametotoro/module_6:worker-v1` from Docker Hub, with `postgres:16` and `rabbitmq:3.13-management`.

Result on 2026-10-18 (`t3.micro`, Ubuntu 24.04 LTS, `us-west-2`): `init` loaded 30,500 rows and exited
0; db, rabbitmq and web healthy; the page answered at `http://54.187.45.90:8080`; Update Analysis and
Pull Data both ran on the worker (Pull Data inserted 38 new entries, 30,538 in all) and were
acknowledged after their commits. The deployment record, the worker log and the port checks are in
`ec2/EC2_DEPLOYMENT.md`.

## 7. Lint and tests

Run in `module_7/` with the packages from `requirements.txt` installed:

```bash
pylint *.py tests                         # 10.00/10, default settings (so is `pylint --ignore=.venv .`)
nbqa pylint grad-cafe-pipeline.ipynb      # 10.00/10
python -m pytest                          # 46 passed
```

From the repository root, `pylint --ignore=.venv module_7` and `python -m pytest module_7` work too.
Without `--ignore=.venv`, pylint also walks into `module_7/.venv` and lints the installed packages.

The notebook's file name, `grad-cafe-pipeline`, has hyphens, and pylint's module-name check expects
snake_case. The first cell turns off that one check for the module docstring line only
(`# pylint: disable=invalid-name` ... `# pylint: enable=invalid-name`); every other check stays on.

The tests never contact AWS. They clear the machine's AWS settings, set fake keys, and use moto's
in-memory S3, with a bucket named after `s3_fetch.DEFAULT_BUCKET`, so they keep passing after the
bucket's name is changed there:

* `tests/test_s3_fetch.py`: downloads, bucket choice, missing bucket or key, bad content, a short
  download, no network, no credentials, a named profile, the command line;
* `tests/test_notebook.py`: the notebook's code cells, run in order as one script, write
  `applicant_data_SM.json`;
* `tests/test_ec2_compose.py`: the compose file uses published images, publishes only 8080 and the
  loopback-only RabbitMQ UI, keeps PostgreSQL in a named volume, and takes every password from `.env`;
* `tests/test_no_secrets.py`: no AWS access key, secret key or private key in any text file here
  (virtual environments are skipped: third-party packages ship example keys).

## 8. Security notes

* No credentials in the code or the notebook. On SageMaker the execution role supplies them; on a
  laptop they live in `~/.aws/`, outside the repository.
* `dailyWork-ms` has no access key (2.6). If one is created, its CSV stays out of the repository
  (`*accessKeys.csv` is in the root `.gitignore`), and the key is deactivated and deleted in IAM when
  it is no longer needed.
* The bucket blocks all public access. The notebook's role gets object access to this bucket through
  the policy made for it ("Specific S3 buckets"). The `AmazonSageMakerFullAccess` policy that is also
  attached to the role lets it list every bucket, read or write objects in buckets whose name
  contains "sagemaker", and read objects tagged `SageMaker=true`. So the role's object access is
  limited to this bucket (apart from those cases), but it can list other buckets.
* `IAMFullAccess` on `dailyWork-ms` allows any IAM change, including attaching `AdministratorAccess`
  to itself, so a stolen `dailyWork-ms` password or key is close to a stolen root login; that is one
  more reason for MFA on the user and for deleting the access key when done. It is attached
  because it lets the user create the notebook's role and its own access key without
  `AdministratorAccess`. The narrower
  choice is `IAMReadOnlyAccess` in its place: root then creates the execution role (2.5, the "no
  Specific S3 buckets" path) and the user's access key and MFA device, and `dailyWork-ms` picks
  **Use existing role** in the notebook form.
* The EC2 key pair (`*.pem`) stays in `~/.ssh/` on the laptop and is git-ignored.
* The real `.env` for EC2 is made in a folder outside the repository (`~/gradcafe-ec2`), and `.env` is
  git-ignored anyway. Only `ec2/.env.example` with `change-me` placeholders is committed.
* The security group allows 22 and 8080 from my IP only. PostgreSQL and AMQP are never published, and
  the RabbitMQ UI listens on the instance's `127.0.0.1` only.
* The EC2 instance has no IAM role attached: the stack does not call AWS.

## 9. AWS resources stopped after the assignment

Both instances are billed by the hour while they run. They are **stopped, not deleted**, because
Module 8 uses them again.

| Resource | State | Date stopped |
| --- | --- | --- |
| SageMaker notebook instance `s3-to-sagemaker-grad-cafe-pipeline` (`ml.t3.medium`) | Stopped | 2026-10-18 |
| EC2 instance `i-0728e111a4728989d` (`gradcafe-module7`, `t3.micro`) | Stopped | 2026-10-18 |

A stopped notebook instance and a stopped EC2 instance still keep their storage volumes, which cost a
small amount per GB-month (section 2.1). The S3 bucket stays as it is.

## 10. Submission

The Canvas zip is made from the commit that is pushed, not from the folder, so it matches GitHub and
cannot pick up git-ignored files (`.venv/`, `applicant_data_SM.json`, `.pytest_cache/`, a `.env`, a
`.pem` or an access key CSV saved here by mistake). At the top of the repository, after the nine
screenshots and the executed notebook are committed and pushed:

```bash
git status --short module_7          # must print nothing
git archive --format=zip -o /mnt/c/Users/<WINDOWS_USER>/Desktop/module_7.zip HEAD module_7
unzip -l /mnt/c/Users/<WINDOWS_USER>/Desktop/module_7.zip | grep -E '\.env$|\.pem|\.csv|applicant_data_SM|\.venv' || echo clean
```
