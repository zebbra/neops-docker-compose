# Generate custom values
This is about a python script to generate a docker-compose file with your custom modifications to be able to update your setup form the git files.

## Build Container
```shell
docker build --platform linux/amd64 --tag quay.io/zebbra/neops-core:dc-custom-values .
```
and push
```shell
docker push quay.io/zebbra/neops-core:dc-custom-values
```

## Usage
Change directory to your `neops-docker-compose` root dir
```shell
docker run -it --rm -v $(pwd)/:/app/ndc quay.io/zebbra/neops-core:dc-custom-values
```