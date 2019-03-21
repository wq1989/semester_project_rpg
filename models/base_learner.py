import os
import re
import sys
import time
from itertools import count
import math
import random
import numpy as np
import tensorflow as tf
from tensorflow.python.summary import summary as tf_summary
from tensorflow.python.keras import callbacks
from tensorflow.python.keras.losses import MeanSquaredError
from tensorflow.python.keras.optimizers import Adam, SGD
from .nets import vel_cnn as prediction_network
from utils import plot_regression_predictions
from data import DirectoryIterator
from data.data_utils import get_mnist_datasets
from data.euroc_utils import load_euroc_dataset, generate_cnn_testing_dataset
from models.custom_callback_fx import CustomModelCheckpoint

#############################################################################
# IMPORT HERE A LIBRARY TO PRODUCE ALL THE FILENAMES (and optionally labels)#
# OF YOUR DATASET. HAVE A LOOK AT `DirectoryIterator' FOR AN EXAMPLE        #
#############################################################################
sys.path.append("../")


class Learner(object):
    def __init__(self, config):
        self.config = config
        self.regressor_model = None
        self.model_name = None
        self.last_epoch_number = 0
        self.trained_model_dir = ""
        pass

    def preprocess_image(self, image):
        ##############################
        # DO YOUR PREPROCESSING HERE #
        ##############################
        """ Preprocess an input image.
        Args:
            image: A uint8 tensor
        Returns:
            image: A preprocessed float32 tensor.
        """
        image = tf.image.decode_jpeg(image)
        image = tf.image.resize(image, [self.config.img_height, self.config.img_width])
        image = tf.cast(image, dtype=tf.float32)
        image = tf.divide(image, 255.0)
        return image

    def read_image_from_dir(self, image_dir):
        image = tf.io.read_file(image_dir)
        return self.preprocess_image(image)

    def read_from_disk(self, inputs_queue):
        """Consumes the inputs queue.
        Args:
            inputs_queue: A scalar string tensor.
        Returns:
            Two tensors: the decoded images, and the labels.
        """
        pnt_seq = tf.strings.to_number(inputs_queue[1], out_type=tf.dtypes.int32)
        file_content = inputs_queue[0].map(self.read_image_from_dir)
        image_seq = tf.image.decode_png(file_content, channels=3)

        return image_seq, pnt_seq

    @staticmethod
    def get_filenames_list(directory):
        """ This function should return all the filenames of the
            files you want to train on.
            In case of classification, it should also return labels.

            Args:
                directory: dataset directory
            Returns:
                List of filenames, [List of associated labels]
        """
        iterator = DirectoryIterator(directory, shuffle=False)
        return iterator.filenames, iterator.ground_truth

    @staticmethod
    def l2_loss(y_true, y_pred):
        return tf.abs(tf.math.subtract(tf.cast(y_true, tf.float32), y_pred))

    def custom_backprop(self, training_ds, validation_ds, ds_lengths, epoch):

        optimizer = tf.keras.optimizers.Adam(self.config.learning_rate, self.config.beta1)

        for i, (x, y) in enumerate(training_ds):

            if i % 100 == 0:
                self.evaluate_model(validation_ds, ds_lengths[1], i, epoch)

            with tf.GradientTape() as tape:
                # Forward pass
                logit = self.regressor_model(tf.cast(x, tf.float32))

                # External loss calculation
                loss = self.l2_loss(y, logit)

                # Manual loss combination:
                loss += sum(self.regressor_model.losses)

            if i % 10 == 0:
                tf.print("Batch {0} of {1}".format(i, ds_lengths[0]))
                tf.print("Training loss of batch {0}/{2} is: {1}".format(i, loss, ds_lengths[0]))

            # Get gradients
            gradient = tape.gradient(loss, self.regressor_model.trainable_weights)

            # Update weights of layer
            optimizer.apply_gradients(zip(gradient, self.regressor_model.trainable_weights))

    def build_and_compile_model(self):
        # model = prediction_network(input_shape=(self.config.img_height, self.config.img_width, 1),
        #                            l2_reg_scale=self.config.l2_reg_scale,
        #                            output_dim=self.config.output_dim)

        model = prediction_network(self.config.l2_reg_scale)

        print(model.summary())
        with tf.name_scope("compile_model"):
            model.compile(optimizer=Adam(self.config.learning_rate, self.config.beta1),
                          loss=self.l2_loss,
                          metrics=['mse'])
        self.regressor_model = model

    def get_dataset(self, dataset_name):

        if dataset_name == 'mnist':
            return get_mnist_datasets(self.config.img_height, self.config.img_width, self.config.batch_size)
        if dataset_name == 'euroc':
            imu_seq_len = 200
            return load_euroc_dataset(self.config.train_dir, self.config.batch_size, imu_seq_len,
                                      self.config.euroc_data_filename_train, self.config.euroc_data_filename_test,
                                      self.config.processed_train_ds, self.trained_model_dir)

    def collect_summaries(self):
        """Collects all summaries to be shown in the tensorboard"""

        #######################################################
        # ADD HERE THE VARIABLES YOU WANT TO SEE IN THE BOARD #
        #######################################################
        tf_summary.scalar("train_loss", self.total_loss, collections=["step_sum"])
        tf_summary.scalar("accuracy", self.accuracy, collections=["step_sum"])
        tf_summary.histogram("logits_distribution", self.logits, collections=["step_sum"])
        tf_summary.histogram("predicted_out_distributions", tf.argmax(self.logits, 1), collections=["step_sum"])
        tf_summary.histogram("ground_truth_distribution", self.labels, collections=["step_sum"])

        ###################################################
        # LEAVE UNCHANGED (gradients and tensors summary) #
        ###################################################
        for var in tf.compat.v1.trainable_variables():
            tf_summary.histogram(var.op.name + "/values", var, collections=["step_sum"])
        for grad, var in self.grads_and_vars:
            tf_summary.histogram(var.op.name + "/gradients", grad, collections=["step_sum"])
        self.step_sum = tf.summary.merge(tf.compat.v1.get_collection('step_sum'))

        ####################
        # VALIDATION ERROR #
        ####################
        self.validation_loss = tf.compat.v1.placeholder(tf.float32, [])
        self.validation_accuracy = tf.compat.v1.placeholder(tf.float32, [])
        tf_summary.scalar("Validation_Loss", self.validation_loss, collections=["validation_summary"])
        tf_summary.scalar("Validation_Accuracy", self.validation_accuracy, collections=["validation_summary"])
        self.val_sum = tf_summary.merge(tf.compat.v1.get_collection('validation_summary'))

    def save(self, sess, checkpoint_dir, step):
        model_name = 'model'
        print(" [*] Saving checkpoint to {}/model-{}".format(checkpoint_dir, step))
        if step == 'best':
            self.saver.save(sess, os.path.join(checkpoint_dir, model_name + '.best'))
        else:
            self.saver.save(sess, os.path.join(checkpoint_dir, model_name), global_step=step)

    def train(self):
        self.build_and_compile_model()

        # Identify last version of trained model
        regex = self.config.model_name + r"_[0-9]*"
        files = [f for f in os.listdir(self.config.checkpoint_dir) if re.match(regex, f)]
        files.sort(key=str.lower)
        if not files:
            model_number = self.config.model_name + "_0"
        else:
            model_number = self.config.model_name + '_' + str(int(files[-1].split('_')[-1]) + 1)

        # Resume training vs new training decision
        if self.config.resume_train:
            print("Resume training from previous checkpoint")
            try:
                model_number = self.recover_model_from_checkpoint(self.config.resume_train_model_number)
            except FileNotFoundError:
                print("Model not found. Creating new model")
        else:
            self.build_and_compile_model()
            os.mkdir(self.config.checkpoint_dir + model_number)

        self.trained_model_dir = self.config.checkpoint_dir + model_number

        # Get training and validation datasets
        train_ds, validation_ds, ds_lengths = self.get_dataset('euroc')

        val_ds_splits = np.diff(np.linspace(0, ds_lengths[1], 2)/self.config.batch_size).astype(np.int)
        val_ds = {}

        for i, split in enumerate(val_ds_splits):
            val_ds[i] = validation_ds.take(split)
            validation_ds = validation_ds.skip(split)

        train_steps_per_epoch = int(math.ceil(ds_lengths[0]/self.config.batch_size))
        val_steps_per_epoch = int(math.ceil(val_ds_splits[0]))

        keras_callbacks = [
            callbacks.EarlyStopping(patience=5, monitor='val_loss'),
            callbacks.TensorBoard(log_dir=self.config.checkpoint_dir + model_number + "/keras", histogram_freq=5),
            CustomModelCheckpoint(
                filepath=os.path.join(
                    self.config.checkpoint_dir + model_number, self.config.model_name + "_{epoch:02d}.h5"),
                save_weights_only=True, verbose=1, period=self.config.save_freq,
                extra_epoch_number=self.last_epoch_number),
        ]

        # for epoch in range(self.config.max_epochs):
        #     self.custom_backprop(val_ds[0], val_ds[0], (val_ds_splits[0], val_ds_splits[0]), epoch)

        # Train!
        history = self.regressor_model.fit(
            val_ds[0].repeat(),
            verbose=2,
            epochs=self.config.max_epochs,
            steps_per_epoch=val_steps_per_epoch,
            validation_data=val_ds[0],
            validation_steps=val_steps_per_epoch,
            callbacks=keras_callbacks)

        # Evaluate model in validation set and entire training set
        self.evaluate_model(val_ds[0], val_ds_splits[0])
        self.evaluate_model(train_ds, ds_lengths[0]/self.config.batch_size)

    def recover_model_from_checkpoint(self, model_number):
        """
        Loads the weights of the default model from the checkpoint files
        """

        # Directory from where to load the saved weights of the model
        model_number_dir = self.config.model_name + "_" + str(model_number)
        recovered_model = self.config.checkpoint_dir + model_number_dir

        regex = self.config.model_name + r"_[0-9]*.h5"
        files = [f for f in os.listdir(recovered_model) if re.match(regex, f)]
        files.sort(key=str.lower)
        if not files:
            raise FileNotFoundError()

        latest_model = files[-1]

        tf.print("Loading weights from ", recovered_model + '/' + latest_model)
        self.regressor_model.load_weights(recovered_model + '/' + latest_model)

        # Get last epoch of training of the model
        self.last_epoch_number = int(latest_model.split(self.config.model_name)[1].split('.')[0].split('_')[1])

        return model_number_dir

    def evaluate_model(self, testing_ds=None, steps=None, i=None, epoch=None, model_number_dir=None):

        if testing_ds is None:
            test_ds, steps = generate_cnn_testing_dataset(self.config.test_dir,
                                                          self.config.euroc_data_filename_train,
                                                          self.config.batch_size,
                                                          self.config.checkpoint_dir + model_number_dir)
            steps = steps / self.config.batch_size
        else:
            test_ds = testing_ds.take(steps)

        predictions = self.regressor_model.predict(test_ds, verbose=1, steps=steps)

        plot_regression_predictions(test_ds, predictions, i, epoch)

    def epoch_end_callback(self, sess, sv, epoch_num):
        # Evaluate val accuracy
        val_loss = 0
        val_accuracy = 0
        for i in range(self.val_steps_per_epoch):
            loss, accuracy = sess.run([self.total_loss, self.accuracy],
                             feed_dict={self.is_training: False})
            val_loss+= loss
            val_accuracy += accuracy
        val_loss = val_loss / self.val_steps_per_epoch
        val_accuracy = val_accuracy / self.val_steps_per_epoch
        # Log to Tensorflow board
        val_sum = sess.run(self.val_sum, feed_dict ={
            self.validation_loss: val_loss,
            self.validation_accuracy: val_accuracy})
        sv.summary_writer.add_summary(val_sum, epoch_num)
        print("Epoch [{}] Validation Loss: {} Validation Accuracy: {}".format(
            epoch_num, val_loss, val_accuracy))
        # Model Saving
        if val_loss < self.min_val_loss:
            self.save(sess, self.config.checkpoint_dir, 'best')
            self.min_val_loss = val_loss
        if epoch_num % self.config.save_freq == 0:
            self.save(sess, self.config.checkpoint_dir, epoch_num)

    def build_test_graph(self):
        """This graph will be used for testing. In particular, it will
           compute the loss on a testing set, or some other utilities.
           Here, data will be passed though placeholders and not via
           input queues.
        """
        ##################################################################
        # UNCHANGED FOR CLASSIFICATION. ADAPT THE INPUT TO OTHER PROBLEMS#
        ##################################################################
        image_height, image_width = self.config.test_img_height, \
                                    self.config.test_img_width
        input_uint8 = tf.placeholder(tf.uint8, [None, image_height,
                                    image_width, 3], name='raw_input')
        input_mc = self.preprocess_image(input_uint8)

        gt_labels = tf.placeholder(tf.uint8, [None], name='gt_labels')
        input_labels = tf.cast(gt_labels, tf.int32)

        ################################################
        # DONT CHANGE NAMESCOPE (NECESSARY FOR LOADING)#
        ################################################
        with tf.name_scope("CNN_prediction"):
            logits = prediction_network(input_mc,
                    l2_reg_scale=self.config.l2_reg_scale, is_training=False,
                    output_dim=self.config.output_dim)

        ###########################################
        # ADAPT TO YOUR LOSSES OR TESTING METRICS #
        ###########################################

        with tf.name_scope("compute_loss"):
            loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=input_labels, logits= logits)
            loss = tf.reduce_mean(loss)

        with tf.name_scope("accuracy"):
            pred_out = tf.cast(tf.argmax(logits, 1), tf.int32)
            correct_prediction = tf.equal(input_labels, pred_out)
            accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))

        ################################################################
        # PUT HERE THE PLACEHOLDERS YOU NEED TO USE, AND OPERATIONS YOU#
        # WANT TO EVALUATE                                             #
        ################################################################
        self.inputs = input_uint8
        self.gt_labels = gt_labels
        self.total_loss = loss
        self.predictions = pred_out
        self.accuracy = accuracy

    def setup_inference(self, config):
        """Sets up the inference graph.
        Args:
            config: config dictionary.
        """
        self.config = config
        self.build_test_graph()

    def inference(self, inputs, sess):
        """Outputs a dictionary with the results of the required operations.
        Args:
            inputs: Dictionary with variable to be feed to placeholders
            sess: current session
        Returns:
            results: dictionary with output of testing operations.
        """
        ################################################################
        # CHANGE INPUTS TO THE PLACEHOLDER YOU NEED, AND OUTPUTS TO THE#
        # RESULTS OF YOUR OPERATIONS                                   #
        ################################################################
        results = {}
        results['loss'], results['accuracy'] = sess.run([self.total_loss,
                self.accuracy], feed_dict= {self.inputs: inputs['images'],
                                            self.gt_labels: inputs['labels']})
        return results
