from setuptools import setup, find_namespace_packages
from tethys_apps.app_installation import find_resource_files


### Apps Definition ###
app_package = 'gw'
release_package = 'tethysapp-' + app_package

# -- Get Resource File -- #
resource_files = find_resource_files('tethysapp/' + app_package + '/templates')
resource_files += find_resource_files('tethysapp/' + app_package + '/public')


setup(
    name=release_package,
    version='1.0.0',
    description='This application uses spatial and temporal interpolation of well data to create groundwater level maps and time series.',
    long_description='',
    keywords='',
    author='Steven Evans',
    author_email='stevenwevans2@gmail.com',
    url='',
    license='',
    packages=find_namespace_packages(),
    package_data={'': resource_files},
    include_package_data=True,
    zip_safe=False,
    install_requires=[]
)
