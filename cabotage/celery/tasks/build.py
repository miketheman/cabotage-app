import datetime
import os
import re
import secrets
import shlex
import subprocess

from celery import shared_task
from base64 import b64encode, b64decode

import kubernetes
import requests
import toml

from tempfile import (
    TemporaryDirectory,
)

import procfile

from dockerfile_parse import DockerfileParser
from flask import current_app
from dxf import DXF

from cabotage.celery.tasks.deploy import run_deploy, run_job

from cabotage.server import (
    db,
    github_app,
    config_writer,
    kubernetes as kubernetes_ext,
)

from cabotage.server.models.projects import (
    activity_plugin,
    Image,
    Release,
    Deployment,
)

from cabotage.utils.docker_auth import (
    generate_docker_registry_jwt,
    generate_kubernetes_imagepullsecrets,
)

from cabotage.utils.logs import filter_secrets
from cabotage.utils.release_build_context import RELEASE_DOCKERFILE_TEMPLATE
from cabotage.utils.github import post_deployment_status_update

Activity = activity_plugin.activity_cls


class BuildError(RuntimeError):
    pass


def build_release_buildkit(release):
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    registry = current_app.config["REGISTRY_BUILD"]
    registry_secure = current_app.config["REGISTRY_SECURE"]
    registry_ca = current_app.config["REGISTRY_VERIFY"]
    buildkitd_url = current_app.config["BUILDKITD_URL"]
    buildkitd_ca = current_app.config["BUILDKITD_VERIFY"]

    process_commands = "\n".join(
        [
            (
                f"COPY envconsul-{process_name}.hcl "
                f"/etc/cabotage/envconsul-{process_name}.hcl"
            )
            for process_name in release.envconsul_configurations
        ]
    )
    release.dockerfile = RELEASE_DOCKERFILE_TEMPLATE.format(
        registry=registry, image=release.image_object, process_commands=process_commands
    )
    db.session.add(release)
    db.session.commit()

    insecure_reg = ""
    registry_url = f"https://{registry}/v2"
    if not registry_secure:
        insecure_reg = ",registry.insecure=true"
        registry_url = f"http://{registry}/v2"

    dockerconfigjson = generate_kubernetes_imagepullsecrets(
        secret=secret,
        registry_urls=[registry_url],
        resource_type="repository",
        resource_name=release.repository_name,
        resource_actions=["push", "pull"],
    )
    buildkitd_toml = toml.dumps(
        {
            "registry": {
                registry: {
                    "insecure": not registry_secure,
                    "ca": [
                        ca
                        for ca in [registry_ca]
                        if registry_secure and not isinstance(ca, bool)
                    ],
                }
            },
        }
    )

    buildctl_command = [
        "buildctl-daemonless.sh",
    ]
    buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--output",
        (
            f"type=image,name={registry}/{release.repository_name}"
            f":release-{release.version},push=true{insecure_reg}"
        ),
    ]

    if registry_ca and not isinstance(registry_ca, bool):
        buildctl_args.append("--registry-auth-tlscacert")
        buildctl_args.append(registry_ca)

    db.session.add(release)
    try:
        if current_app.config["KUBERNETES_ENABLED"]:
            buildctl_args += [
                "--local",
                "dockerfile=/context",
                "--local",
                "context=/context",
            ]
            secret_object = kubernetes.client.V1Secret(
                type="kubernetes.io/dockerconfigjson",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkit-registry-auth-{release.build_job_id}",
                ),
                data={
                    ".dockerconfigjson": b64encode(dockerconfigjson.encode()).decode(),
                },
            )
            buildkitd_toml_configmap_object = kubernetes.client.V1ConfigMap(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkitd-toml-{release.build_job_id}",
                ),
                data={
                    "buildkitd.toml": buildkitd_toml,
                },
            )
            context_configmap_object = release.release_build_context_configmap
            job_object = kubernetes.client.V1Job(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"releasebuild-{release.build_job_id}",
                    labels={
                        "organization": release.application.project.organization.slug,
                        "project": release.application.project.slug,
                        "application": release.application.slug,
                        "process": "build",
                        "build_id": release.build_job_id,
                    },
                ),
                spec=kubernetes.client.V1JobSpec(
                    active_deadline_seconds=1800,
                    backoff_limit=0,
                    parallelism=1,
                    completions=1,
                    template=kubernetes.client.V1PodTemplateSpec(
                        metadata=kubernetes.client.V1ObjectMeta(
                            labels={
                                "organization": release.application.project.organization.slug,  # noqa: E501
                                "project": release.application.project.slug,
                                "application": release.application.slug,
                                "process": "build",
                                "build_id": release.build_job_id,
                                "ca-admission.cabotage.io": "true",
                            },
                            annotations={
                                "container.apparmor.security.beta.kubernetes.io/build": "unconfined",  # noqa: E501
                            },
                        ),
                        spec=kubernetes.client.V1PodSpec(
                            restart_policy="Never",
                            containers=[
                                kubernetes.client.V1Container(
                                    name="build",
                                    image="cabotage/buildkit:4-rootless",
                                    command=buildctl_command,
                                    args=buildctl_args,
                                    env=[
                                        kubernetes.client.V1EnvVar(
                                            name="BUILDKITD_FLAGS",
                                            value="--config /home/user/.config/buildkit/buildkitd.toml --oci-worker-no-process-sandbox",  # noqa: E501
                                        ),
                                    ],
                                    security_context=kubernetes.client.V1SecurityContext(
                                        seccomp_profile=kubernetes.client.V1SeccompProfile(
                                            type="Unconfined",
                                        ),
                                        run_as_user=1000,
                                        run_as_group=1000,
                                    ),
                                    volume_mounts=[
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.local/share/buildkit",
                                            name="buildkitd",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.config/buildkit",
                                            name="buildkitd-toml",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.docker",
                                            name="buildkit-registry-auth",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/context/Dockerfile",
                                            sub_path="Dockerfile",
                                            name="build-context",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/context/entrypoint.sh",
                                            sub_path="entrypoint.sh",
                                            name="build-context",
                                        ),
                                        *[
                                            kubernetes.client.V1VolumeMount(
                                                mount_path=f"/context/envconsul-{process_name}.hcl",
                                                sub_path=f"envconsul-{process_name}.hcl",
                                                name="build-context",
                                            )
                                            for process_name in release.envconsul_configurations  # noqa: E501
                                        ],
                                    ],
                                ),
                            ],
                            volumes=[
                                kubernetes.client.V1Volume(
                                    name="buildkitd",
                                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkitd-toml",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"buildkitd-toml-{release.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key="buildkitd.toml",
                                                path="buildkitd.toml",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkit-registry-auth",
                                    secret=kubernetes.client.V1SecretVolumeSource(
                                        secret_name=f"buildkit-registry-auth-{release.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key=".dockerconfigjson",
                                                path="config.json",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="build-context",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"build-context-{release.build_job_id}"
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            )

            api_client = kubernetes_ext.kubernetes_client
            core_api_instance = kubernetes.client.CoreV1Api(api_client)
            batch_api_instance = kubernetes.client.BatchV1Api(api_client)
            core_api_instance.create_namespaced_config_map(
                "default", context_configmap_object
            )
            core_api_instance.create_namespaced_config_map(
                "default", buildkitd_toml_configmap_object
            )
            core_api_instance.create_namespaced_secret("default", secret_object)

            try:
                job_complete, job_logs = run_job(
                    core_api_instance, batch_api_instance, "default", job_object
                )
            finally:
                core_api_instance.delete_namespaced_secret(
                    f"buildkit-registry-auth-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"buildkitd-toml-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"build-context-{release.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )

            release.release_build_log = filter_secrets(job_logs)
            db.session.commit()
            db.session.flush()
            if not job_complete:
                raise BuildError("Image build failed!")
        else:
            buildctl_args += [
                "--local",
                "dockerfile=context",
                "--local",
                "context=context",
            ]
            context_configmap_object = release.release_build_context_configmap
            buildctl_command = ["buildctl"]
            if buildkitd_ca is not None:
                buildctl_args.insert(0, f"--tlscacert={buildkitd_ca}")
            with TemporaryDirectory() as tempdir:
                os.makedirs(os.path.join(tempdir, "context"), exist_ok=True)
                for file, contents in context_configmap_object.data.items():
                    with open(os.path.join(tempdir, "context", file), "w") as f:
                        f.write(contents)
                os.makedirs(os.path.join(tempdir, ".docker"), exist_ok=True)
                with open(os.path.join(tempdir, ".docker", "config.json"), "w") as f:
                    f.write(dockerconfigjson)
                try:
                    completed_subprocess = subprocess.run(
                        buildctl_command + buildctl_args,
                        env={"BUILDKIT_HOST": buildkitd_url, "HOME": tempdir},
                        cwd=tempdir,
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except subprocess.CalledProcessError as exc:
                    raise BuildError(f"Build subprocess failed: {exc}")
            release.release_build_log = (
                " ".join(buildctl_command + buildctl_args)
                + "\n"
                + completed_subprocess.stdout
            )
            db.session.commit()
    except Exception as exc:
        raise BuildError(f"Build failed: {exc}")

    def auth(dxf, response):
        dxf.token = generate_docker_registry_jwt(
            access=[
                {
                    "type": "repository",
                    "name": release.repository_name,
                    "actions": ["pull"],
                }
            ]
        )

    try:
        _tlsverify = False
        if registry_secure:
            _tlsverify = registry_ca
            if _tlsverify == "True":
                _tlsverify = True
        client = DXF(
            host=registry,
            repo=release.repository_name,
            auth=auth,
            insecure=(not registry_secure),
            tlsverify=_tlsverify,
        )
        pushed_release = client.get_digest(f"release-{release.version}")
    except Exception as exc:
        raise BuildError(f"Release push failed: {exc}")

    return {
        "release_id": pushed_release,
    }


def _fetch_github_file(
    github_repository="owner/repo", ref="main", access_token=None, filename="Dockerfile"
):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if access_token is not None:
        headers["Authorization"] = f"token {access_token}"
    response = requests.get(
        f"https://api.github.com/repos/{github_repository}/contents/{filename}",
        params={"ref": ref},
        headers=headers,
    )
    if response.status_code == 404:
        return None
    if response.status_code == 200:
        data = response.json()
        if data["encoding"] == "base64":
            return b64decode(response.json()["content"]).decode()
    response.raise_for_status()


def _fetch_commit_sha_for_ref(
    github_repository="owner/repo", ref="main", access_token=None
):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if access_token is not None:
        headers["Authorization"] = f"token {access_token}"
    response = requests.get(
        f"https://api.github.com/repos/{github_repository}/commits/{ref}",
        params={"ref": ref},
        headers=headers,
    )
    if response.status_code == 404:
        return None
    if response.status_code == 200:
        return response.json()["sha"]
    response.raise_for_status()


def build_image_buildkit(image=None):
    secret = current_app.config["REGISTRY_AUTH_SECRET"]
    registry = current_app.config["REGISTRY_BUILD"]
    registry_secure = current_app.config["REGISTRY_SECURE"]
    registry_ca = current_app.config["REGISTRY_VERIFY"]
    buildkitd_url = current_app.config["BUILDKITD_URL"]
    buildkitd_ca = current_app.config["BUILDKITD_VERIFY"]

    access_token = None
    # TODO: Do the GitHub Dance we'll want to auth if we ever do private repoz
    # bearer_token = github_app.bearer_token
    # access_token_response = requests.post(
    #    f'https://api.github.com/app/installations/{installation_id}/access_tokens',
    #    headers={
    #        'Accept': 'application/vnd.github.machine-man-preview+json',
    #        'Authorization': f'Bearer {bearer_token}',
    #    }
    # )
    # if 'token' not in access_token_response.json():
    #    print(f'Unable to authenticate for {installation_id}')
    #    print(access_token_response.json())
    #    raise BuildError(f'Unable to authenticate for {installation_id}')
    # access_token = access_token_response.json()

    if image.commit_sha == "null":
        commit_sha = _fetch_commit_sha_for_ref(
            image.application.github_repository,
            image.build_ref,
            access_token=access_token,
        )
        if image.image_metadata is None:
            image.image_metadata = {"sha": commit_sha}
        else:
            image.image_metadata["sha"] = commit_sha

    dockerfile_name = None
    dockerfile_body = _fetch_github_file(
        image.application.github_repository,
        image.commit_sha,
        access_token=access_token,
        filename="Dockerfile.cabotage",
    )
    dockerfile_name = "Dockerfile.cabotage"
    if dockerfile_body is None:
        dockerfile_body = _fetch_github_file(
            image.application.github_repository,
            image.commit_sha,
            access_token=access_token,
            filename="Dockerfile",
        )
        dockerfile_name = "Dockerfile"
    if dockerfile_body is None:
        raise BuildError(
            "No Dockerfile.cabotage or Dockerfile found in root of "
            f"{image.application.github_repository}@{image.commit_sha}"
        )

    procfile_body = _fetch_github_file(
        image.application.github_repository,
        image.commit_sha,
        access_token=access_token,
        filename="Procfile.cabotage",
    )
    if procfile_body is None:
        procfile_body = _fetch_github_file(
            image.application.github_repository,
            image.commit_sha,
            access_token=access_token,
            filename="Procfile",
        )
    if procfile_body is None:
        raise BuildError(
            "No Procfile.cabotage or Procfile found in root of "
            "{image.application.github_repository}@{image.commit_sha}"
        )

    image.dockerfile = dockerfile_body
    image.procfile = procfile_body
    db.session.commit()

    dockerfile_object = DockerfileParser()
    with TemporaryDirectory() as tempdir:
        previous_dir = os.getcwd()
        os.chdir(tempdir)
        try:
            dockerfile_object.content = dockerfile_body
        finally:
            os.chdir(previous_dir)
    dockerfile_env_vars = list(dockerfile_object.envs.keys())
    try:
        processes = procfile.loads(procfile_body)
    except ValueError as exc:
        raise BuildError(f"error parsing Procfile: {exc}")

    for process_name in processes.keys():
        if re.search("\s", process_name) is not None:
            raise BuildError(
                f'Invalid process name: "{process_name}" in Procfile, '
                "may not contain whitespace."
            )

    insecure_reg = ""
    registry_url = f"https://{registry}/v2"
    if not registry_secure:
        insecure_reg = ",registry.insecure=true"
        registry_url = f"http://{registry}/v2"

    dockerconfigjson = generate_kubernetes_imagepullsecrets(
        secret=secret,
        registry_urls=[registry_url],
        resource_type="repository",
        resource_name=image.repository_name,
        resource_actions=["push", "pull"],
    )
    buildkitd_toml = toml.dumps(
        {
            "registry": {
                registry: {
                    "insecure": not registry_secure,
                    "ca": [
                        ca
                        for ca in [registry_ca]
                        if registry_secure and not isinstance(ca, bool)
                    ],
                }
            },
        }
    )

    buildctl_command = [
        "buildctl-daemonless.sh",
    ]
    buildctl_args = [
        "build",
        "--progress=plain",
        "--frontend",
        "dockerfile.v0",
        "--opt",
        f"filename=./{dockerfile_name}",
        "--opt",
        (
            "context=https://github.com/"
            f"{image.application.github_repository}.git#{image.commit_sha}"
        ),
        "--import-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg}"
        ),
        "--export-cache",
        (
            f"type=registry,ref={registry}/{image.repository_name}"
            f":image-buildcache{insecure_reg},mode=min"
        ),
        "--output",
        (
            f"type=image,name={registry}/{image.repository_name}"
            f":image-{image.version},push=true{insecure_reg}"
        ),
    ]

    for k, v in image.buildargs(config_writer).items():
        buildctl_args.append("--opt")
        buildctl_args.append(shlex.quote(f"build-arg:{k}={v}"))

    if registry_ca and not isinstance(registry_ca, bool):
        buildctl_args.append(registry_ca)
        buildctl_args.append("--registry-auth-tlscacert")

    try:
        if current_app.config["KUBERNETES_ENABLED"]:
            secret_object = kubernetes.client.V1Secret(
                type="kubernetes.io/dockerconfigjson",
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkit-registry-auth-{image.build_job_id}",
                ),
                data={
                    ".dockerconfigjson": b64encode(dockerconfigjson.encode()).decode(),
                },
            )
            buildkitd_toml_configmap_object = kubernetes.client.V1ConfigMap(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"buildkitd-toml-{image.build_job_id}",
                ),
                data={
                    "buildkitd.toml": buildkitd_toml,
                },
            )
            job_object = kubernetes.client.V1Job(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=f"imagebuild-{image.build_job_id}",
                    labels={
                        "organization": image.application.project.organization.slug,
                        "project": image.application.project.slug,
                        "application": image.application.slug,
                        "process": "build",
                        "build_id": image.build_job_id,
                    },
                ),
                spec=kubernetes.client.V1JobSpec(
                    active_deadline_seconds=1800,
                    backoff_limit=0,
                    parallelism=1,
                    completions=1,
                    template=kubernetes.client.V1PodTemplateSpec(
                        metadata=kubernetes.client.V1ObjectMeta(
                            labels={
                                "organization": image.application.project.organization.slug,  # noqa: E501
                                "project": image.application.project.slug,
                                "application": image.application.slug,
                                "process": "build",
                                "build_id": image.build_job_id,
                                "ca-admission.cabotage.io": "true",
                            },
                            annotations={
                                "container.apparmor.security.beta.kubernetes.io/build": "unconfined",  # noqa: E501
                            },
                        ),
                        spec=kubernetes.client.V1PodSpec(
                            restart_policy="Never",
                            containers=[
                                kubernetes.client.V1Container(
                                    name="build",
                                    image="cabotage/buildkit:4-rootless",
                                    command=buildctl_command,
                                    args=buildctl_args,
                                    env=[
                                        kubernetes.client.V1EnvVar(
                                            name="BUILDKITD_FLAGS",
                                            value="--config /home/user/.config/buildkit/buildkitd.toml --oci-worker-no-process-sandbox",  # noqa: E501
                                        ),
                                    ],
                                    security_context=kubernetes.client.V1SecurityContext(
                                        seccomp_profile=kubernetes.client.V1SeccompProfile(
                                            type="Unconfined",
                                        ),
                                        run_as_user=1000,
                                        run_as_group=1000,
                                    ),
                                    volume_mounts=[
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.local/share/buildkit",
                                            name="buildkitd",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.config/buildkit",
                                            name="buildkitd-toml",
                                        ),
                                        kubernetes.client.V1VolumeMount(
                                            mount_path="/home/user/.docker",
                                            name="buildkit-registry-auth",
                                        ),
                                    ],
                                ),
                            ],
                            volumes=[
                                kubernetes.client.V1Volume(
                                    name="buildkitd",
                                    empty_dir=kubernetes.client.V1EmptyDirVolumeSource(),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkitd-toml",
                                    config_map=kubernetes.client.V1ConfigMapVolumeSource(
                                        name=f"buildkitd-toml-{image.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key="buildkitd.toml",
                                                path="buildkitd.toml",
                                            ),
                                        ],
                                    ),
                                ),
                                kubernetes.client.V1Volume(
                                    name="buildkit-registry-auth",
                                    secret=kubernetes.client.V1SecretVolumeSource(
                                        secret_name=f"buildkit-registry-auth-{image.build_job_id}",
                                        items=[
                                            kubernetes.client.V1KeyToPath(
                                                key=".dockerconfigjson",
                                                path="config.json",
                                            ),
                                        ],
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            )

            api_client = kubernetes_ext.kubernetes_client
            core_api_instance = kubernetes.client.CoreV1Api(api_client)
            batch_api_instance = kubernetes.client.BatchV1Api(api_client)
            core_api_instance.create_namespaced_config_map(
                "default", buildkitd_toml_configmap_object
            )
            core_api_instance.create_namespaced_secret("default", secret_object)

            try:
                job_complete, job_logs = run_job(
                    core_api_instance, batch_api_instance, "default", job_object
                )
            finally:
                core_api_instance.delete_namespaced_secret(
                    f"buildkit-registry-auth-{image.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )
                core_api_instance.delete_namespaced_config_map(
                    f"buildkitd-toml-{image.build_job_id}",
                    "default",
                    propagation_policy="Foreground",
                )

            image.image_build_log = filter_secrets(job_logs)
            db.session.commit()
            db.session.flush()
            if not job_complete:
                raise BuildError("Image build failed!")
        else:
            buildctl_command = ["buildctl"]
            if buildkitd_ca is not None:
                buildctl_args.insert(0, f"--tlscacert={buildkitd_ca}")
            with TemporaryDirectory() as tempdir:
                os.makedirs(os.path.join(tempdir, ".docker"), exist_ok=True)
                with open(os.path.join(tempdir, ".docker", "config.json"), "w") as f:
                    f.write(dockerconfigjson)
                try:
                    completed_subprocess = subprocess.run(
                        buildctl_command + buildctl_args,
                        env={"BUILDKIT_HOST": buildkitd_url, "HOME": tempdir},
                        cwd="/tmp",
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except subprocess.CalledProcessError as exc:
                    raise BuildError(f"Build subprocess failed: {exc}")
            image.image_build_log = (
                " ".join(buildctl_command + buildctl_args)
                + "\n"
                + completed_subprocess.stdout
            )
            db.session.commit()
    except Exception as exc:
        raise BuildError(f"Build failed: {exc}")

    def auth(dxf, response):
        dxf.token = generate_docker_registry_jwt(
            access=[
                {
                    "type": "repository",
                    "name": image.repository_name,
                    "actions": ["pull"],
                }
            ]
        )

    try:
        _tlsverify = False
        if registry_secure:
            _tlsverify = registry_ca
            if _tlsverify == "True":
                _tlsverify = True
        client = DXF(
            host=registry,
            repo=image.repository_name,
            auth=auth,
            insecure=(not registry_secure),
            tlsverify=_tlsverify,
        )
        pushed_image = client.get_digest(f"image-{image.version}")
    except Exception as exc:
        raise BuildError(f"Image push failed: {exc}")

    return {
        "image_id": pushed_image,
        "processes": processes,
        "dockerfile": dockerfile_body,
        "procfile": procfile_body,
        "dockerfile_env_vars": dockerfile_env_vars,
    }


@shared_task()
def run_image_build(image_id=None, buildkit=False):
    current_app.config["REGISTRY_AUTH_SECRET"]
    current_app.config["REGISTRY_BUILD"]
    image = Image.query.filter_by(id=image_id).first()
    if image is None:
        raise KeyError(f"Image with ID {image_id} not found!")

    image.build_job_id = secrets.token_hex(4)
    db.session.add(image)
    db.session.commit()
    try:
        try:
            build_metadata = build_image_buildkit(image)
        except BuildError as exc:
            db.session.add(image)
            image.error = True
            image.error_detail = str(exc)
            db.session.commit()
            raise
    except Exception:
        raise

    db.session.add(image)
    image.image_id = build_metadata["image_id"]
    image.processes = build_metadata["processes"]
    image.built = True
    if image.image_metadata is None:
        image.image_metadata = {
            "dockerfile_env_vars": build_metadata["dockerfile_env_vars"]
        }
    else:
        image.image_metadata["dockerfile_env_vars"] = build_metadata[
            "dockerfile_env_vars"
        ]

    db.session.add(image)
    db.session.commit()

    if (
        image.built
        and image.image_metadata
        and image.image_metadata.get("auto_deploy", False)
    ):
        if (
            "installation_id" in image.image_metadata
            and "statuses_url" in image.image_metadata
        ):
            access_token = github_app.fetch_installation_access_token(
                image.image_metadata["installation_id"]
            )
            post_deployment_status_update(
                access_token,
                image.image_metadata["statuses_url"],
                "pending",
                "Image built, Release build commencing.",
            )
        release = image.application.create_release()
        release.release_metadata = image.image_metadata
        db.session.add(release)
        db.session.flush()
        activity = Activity(
            verb="create",
            object=release,
            data={
                "user_id": "automation",
                "deployment_id": image.image_metadata.get("id", None),
                "description": image.image_metadata.get("description", None),
                "timestamp": datetime.datetime.utcnow().isoformat(),
            },
        )
        db.session.add(activity)
        db.session.commit()
        run_release_build.delay(release_id=release.id)


@shared_task()
def run_release_build(release_id=None):
    try:
        current_app.config["REGISTRY_AUTH_SECRET"]
        current_app.config["REGISTRY_BUILD"]
        release = Release.query.filter_by(id=release_id).first()
        if release is None:
            raise KeyError(f"Release with ID {release_id} not found!")

        release.build_job_id = secrets.token_hex(4)
        db.session.add(release)
        db.session.commit()

        try:
            build_metadata = build_release_buildkit(release)
            release.release_id = build_metadata["release_id"]
            release.built = True
        except BuildError as exc:
            release.error = True
            release.error_detail = str(exc)
        except Exception:
            raise
        db.session.add(release)
        db.session.commit()

        if (
            release.built
            and release.release_metadata
            and release.release_metadata.get("auto_deploy", False)
        ):
            if (
                "installation_id" in release.release_metadata
                and "statuses_url" in release.release_metadata
            ):
                access_token = github_app.fetch_installation_access_token(
                    release.release_metadata["installation_id"]
                )
                post_deployment_status_update(
                    access_token,
                    release.release_metadata["statuses_url"],
                    "pending",
                    "Release built, Deployment commencing.",
                )
            deployment = Deployment(
                application_id=release.application.id,
                release=release.asdict,
                deploy_metadata=release.release_metadata,
            )
            db.session.add(deployment)
            db.session.flush()
            activity = Activity(
                verb="create",
                object=deployment,
                data={
                    "user_id": "automation",
                    "deployment_id": release.release_metadata.get("id", None),
                    "description": release.release_metadata.get("description", None),
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                },
            )
            db.session.add(activity)
            db.session.commit()
            if current_app.config["KUBERNETES_ENABLED"]:
                deployment_id = deployment.id
                run_deploy.delay(deployment_id=deployment.id)
                deployment = Deployment.query.filter_by(id=deployment_id).first()
            else:
                from cabotage.celery.tasks.deploy import fake_deploy_release

                fake_deploy_release(deployment)
                deployment.complete = True
                db.session.commit()
    except Exception:
        raise
