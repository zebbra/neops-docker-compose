# Use an official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.9-slim-buster

# Set the working directory in the Docker container
WORKDIR /app

# Install any necessary dependencies if required
RUN pip install python-dotenv
RUN pip install GitPython
RUN pip install pyyml
RUN pip install deepmerge

# Update and install git
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y git

# Add your Python script to the Docker container
COPY gen_custom_values_yml.py .

# Run my_script.py when the container launches
CMD ["python", "./gen_custom_values_yml.py"]