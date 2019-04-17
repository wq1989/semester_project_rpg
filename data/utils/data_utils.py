import re
import os
import sys
import cv2
import logging
import requests
import scipy.io
import numpy as np
from matplotlib import pyplot as plt
from scipy import signal
from scipy.interpolate import interp1d

from tensorflow.python.keras.preprocessing.image import Iterator
from tensorflow.python.keras import datasets as k_ds
from tensorflow.python.keras.utils import to_categorical
from tensorflow.python.data import Dataset

############################################################################
# EXAMPLE CLASS TO FETCH FILENAMES (AND OPTIONALLY LABELS) FROM DIRECTORIES#
############################################################################


class DirectoryIterator(Iterator):
    """
    Class for managing data loading.of images and labels
    We assume that the folder structure is:
    root_folder/
           folder_1/
                    images/
                    labels.txt
           folder_2/
                    images/
                    labels.txt
           .
           .
           folder_n/
                    images/
                    labels.txt

    # Arguments
       directory: Path to the root directory to read data from.
       target_size: tuple of integers, dimensions to resize input images to.
       batch_size: The desired batch size
       shuffle: Whether to shuffle data or not
       seed : numpy seed to shuffle data
       follow_links: Bool, whether to follow symbolic links or not

    """
    def __init__(self, directory, target_size=(224, 224), batch_size=32, shuffle=True, seed=None, follow_links=False):
        self.directory = directory
        self.target_size = tuple(target_size)
        self.follow_links = follow_links
        self.image_shape = self.target_size + (3,)

        # First count how many experiments are out there
        self.samples = 0

        experiments = []
        for subdir in sorted(os.listdir(directory)):
            if os.path.isdir(os.path.join(directory, subdir)):
                experiments.append(subdir)
        self.num_experiments = len(experiments)
        self.formats = {'png'}
        print("----------------------------------------------------")
        print("Loading the following formats {}".format(self.formats))
        print("----------------------------------------------------")

        # Associate each filename with a corresponding label
        self.filenames = []
        self.ground_truth = []

        for subdir in experiments:
            subpath = os.path.join(directory, subdir)
            try:
                self._decode_experiment_dir(subpath)
            except:
                raise ImportWarning("Image reading in {} failed".format(subpath))

        if self.samples == 0:
            raise IOError("Did not find any file in the dataset folder")

        # Conversion of list into array
        self.ground_truth = np.array(self.ground_truth, dtype = np.uint8)

        print('Found {} images belonging to {} experiments.'.format(self.samples, self.num_experiments))
        super(DirectoryIterator, self).__init__(self.samples, batch_size, shuffle, seed)

    def _recursive_list(self, subpath):
        return sorted(os.walk(subpath, followlinks=self.follow_links), key=lambda tpl: tpl[0])

    def _decode_experiment_dir(self, dir_subpath):
        labels_filename = os.path.join(dir_subpath, "labels.txt")

        # Try load labels
        try:
            ground_truth = np.loadtxt(labels_filename, usecols=0)
        except:
            raise IOError("Labels file was not found found")

        # Now fetch all images in the image subdir
        image_dir_path = os.path.join(dir_subpath, "images")
        for root, _, files in self._recursive_list(image_dir_path):
            sorted_files = sorted(files, key=lambda fname: int(re.search(r'\d+', fname).group()))
            for frame_number, fname in enumerate(sorted_files):
                is_valid = False
                for extension in self.formats:
                    if fname.lower().endswith('.' + extension):
                        is_valid = True
                        break
                if is_valid:
                    absolute_path = os.path.join(root, fname)
                    self.filenames.append(absolute_path)
                    self.ground_truth.append(ground_truth[frame_number])
                    self.samples += 1

    def next(self):
        """
        Public function to fetch next batch. Note that this function
        will only be used for EVALUATION and TESTING, but not for training.

        # Returns
            The next batch of images and labels.
        """
        with self.lock:
            index_array, current_index, current_batch_size = next(
                    self.index_generator)

        # Image transformation is not under thread lock, so it can be done in
        # parallel
        batch_x = np.zeros((current_batch_size,) + self.image_shape, dtype=np.uint8)
        batch_y = np.zeros((current_batch_size,), dtype=np.uint8)

        # Build batch of image data
        for i, j in enumerate(index_array):
            fname = self.filenames[j]
            x = load_img(os.path.join(fname))
            batch_x[i] = x

        batch_y = self.ground_truth[index_array]

        return batch_x, batch_y


def load_img(path, target_size=None):
    """
    Load an image. Ans reshapes it to target size

    # Arguments
        path: Path to image file.
        target_size: Either `None` (default to original size)
            or tuple of ints `(img_width, img_height)`.

    # Returns
        Image as numpy array.
    """
    img = cv2.imread(path)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    return img


def get_mnist_datasets(img_h, img_w, batch_s):

    fashion_mnist = k_ds.fashion_mnist
    (x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()
    x_train, x_test = x_train / 255.0, x_test / 255.0

    # Further break training data into train / validation sets
    (x_train, x_valid) = x_train[5000:], x_train[:5000]
    (y_train, y_valid) = y_train[5000:], y_train[:5000]

    # Reshape input data from (28, 28) to (28, 28, 1)
    w, h = img_w, img_h
    x_train = x_train.reshape(x_train.shape[0], w, h, 1)
    x_valid = x_valid.reshape(x_valid.shape[0], w, h, 1)
    x_test = x_test.reshape(x_test.shape[0], w, h, 1)

    # One-hot encode the labels
    y_train = to_categorical(y_train, 10)
    y_valid = to_categorical(y_valid, 10)
    y_test = to_categorical(y_test, 10)

    train_ds = Dataset.from_tensor_slices((x_train, y_train)).shuffle(batch_s).batch(batch_s).repeat()
    validation_ds = Dataset.from_tensor_slices((x_valid, y_valid)).shuffle(batch_s).batch(batch_s).repeat()
    test_ds = Dataset.from_tensor_slices((x_test, y_test)).shuffle(batch_s).batch(batch_s)
    ds_lengths = (len(x_train), len(x_valid))

    return train_ds, validation_ds, ds_lengths


def get_file_from_url(file_name, link):
    with open(file_name, "wb") as f:
        print("Downloading %s" % file_name)
        response = requests.get(link, stream=True)
        total_length = response.headers.get('content-length')

        if total_length is None:  # no content length header
            f.write(response.content)
        else:
            dl = 0
            total_length = int(total_length)
            for data in response.iter_content(chunk_size=4096):
                dl += len(data)
                f.write(data)
                done = int(50 * dl / total_length)
                sys.stdout.write("\r[%s%s%s]" % ('=' * done, '>', ' ' * (50 - done - 1)))
                sys.stdout.flush()


def save_mat_data(x_data, y_data, file_name):
    """
    Saves a dataset as .mat in the specified directory

    :param x_data: feature data
    :param y_data: label data
    :param file_name: directory for .mat file
    """

    scipy.io.savemat(file_name, mdict={'x': x_data, 'y': y_data}, oned_as='row')


def load_mat_data(directory):
    """
    Loads a dataset saved with the save_processed_dataset_files function

    :param directory: directory of the mat file <dir/file.mat>
    :return: the x and y data, in format [x, y]
    """

    mat_data = scipy.io.loadmat(directory)

    y_tensor = mat_data['y']
    x_tensor = mat_data['x']

    # Reduce dimensionality of y data
    y_aux = np.zeros(y_tensor.shape)
    for i in range(np.shape(y_tensor)[0]):
        for j in range(np.shape(y_tensor)[1]):
            y_aux[i][j] = y_tensor[i][j][0][0]

    return x_tensor, y_aux


def interpolate_ts(ref_ts, target_ts, meas_vec, is_quaternion=False):
    """
    Interpolates a vector to different acquisition times, given the original aquisition times

    :param ref_ts: reference timestamp vector
    :param target_ts: target timestamp vector (must be inside the limits of `ref_ts`)
    :param meas_vec: vector to be interpolated
    :param is_quaternion: whether the vector is a quaternion or not
    :return: the interpolated vector `meas_vec` at times `target_ts`
    """

    if is_quaternion:
        logging.warning("Quaternion SLERP not implemented yet. A quaternion vector was interpolated using the euclidean"
                        " method, which may yield an incorrect result!")
        # TODO: implement SLERP!

    _, d = np.shape(meas_vec)

    # Generate interpolating functions for each component of the vector
    interp_fx = [None for _ in range(d)]
    for i in range(d):
        interp_fx[i] = interp1d(ref_ts, meas_vec[:, i])

    # Initialize array of interpolated Ground Truth velocities
    interp_vec = np.zeros((len(target_ts), d))

    # Fill in array
    for i, imu_timestamp in enumerate(target_ts):
        interp_vec[i, :] = np.array([interp_fx[j](imu_timestamp) for j in range(d)])

    return interp_vec


def filter_with_coeffs(a, b, time_series, sampling_f=None, plot_stft=False):
    """
    Applies a digital filter along a signal using the filter coefficients a, b

    :param a: array of filter coefficients a
    :param b: array of filter coefficients b
    :param time_series: signal to filter
    :param sampling_f: sampling frequency of signal
    :param plot_stft: whether to plot the STFT
    :return:
    """

    if plot_stft:
        assert sampling_f is not None, "A sampling frequency must be specified to plot the STFT"
        plt.figure()
        f, t, stft = signal.stft(time_series[:, 0], sampling_f)
        plt.subplot(2, 1, 1)
        plt.pcolormesh(t, f, np.abs(stft))
        plt.title('STFT Magnitude')
        plt.ylabel('Frequency [Hz]')
        plt.xlabel('Time [sec]')

    filtered_signal = signal.lfilter(b, a, time_series, axis=0)

    if plot_stft:
        f, t, stft = signal.stft(filtered_signal[:, 0], 200)
        plt.subplot(2, 1, 2)
        plt.pcolormesh(t, f, np.abs(stft))
        plt.title('STFT Magnitude')
        plt.ylabel('Frequency [Hz]')
        plt.xlabel('Time [sec]')
        plt.show()

    return filtered_signal


def save_train_and_test_datasets(train_ds_node, test_ds_node, x_data, y_data, test_split, shuffle):
    """
    Saves a copy of the train & test datasets as a mat file in a specified file names

    :param train_ds_node: Training ds file name <dir/file.mat>
    :param test_ds_node: Test ds file name <dir/file.mat>
    :param x_data: x data (samples in first dimension)
    :param y_data: y data (samples in first dimension)
    :param test_split: the percentage of dataset to be split for testing
    :param shuffle: whether datasets should be shuffled
    """
    if os.path.exists(train_ds_node):
        os.remove(train_ds_node)
    os.mknod(train_ds_node)

    if os.path.exists(test_ds_node):
        os.remove(test_ds_node)
    os.mknod(test_ds_node)

    total_ds_len = int(len(y_data))
    test_ds_len = int(np.ceil(total_ds_len * test_split))

    # Choose some entries to separate for the test set
    if shuffle:
        test_indexes = np.random.choice(total_ds_len, test_ds_len, replace=False)
    else:
        test_indexes = range(total_ds_len - test_ds_len, total_ds_len)

    test_set_x = x_data[test_indexes]
    test_set_y = y_data[test_indexes]

    # Remove the test ds entries from train dataset
    train_set_x = np.delete(x_data, test_indexes, axis=0)
    train_set_y = np.delete(y_data, test_indexes, axis=0)

    save_mat_data(train_set_x, train_set_y, train_ds_node)
    save_mat_data(test_set_x, test_set_y, test_ds_node)
