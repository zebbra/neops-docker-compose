# Docker Compose Deploy Setup

see [docs.neops.io](https://docs.neops.io)


## Select required ressources
At the required services/subdirectories:

 ```shell
 cp .env.dist .env
 touch docker-compose.custom-values.yml
 ```

Edit `.evn` file, edit required parameters and take a look at the `COMPOSE_FILE` file string to load the required elements.

## Generate docker-compose.custom-values.yml
Change directory to your `neops-docker-compose` root dir
```shell
docker run -it --rm -v $(pwd)/:/app/ndc quay.io/zebbra/neops-core:dc-custom-values
```
