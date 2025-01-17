import math
import warnings
warnings.filterwarnings("ignore")

import random
import tensorflow as tf
from word_character_embedding import get_batch_word_char_ids, pad_sequences
from general_utils import Progbar, write_file
from model_base import BaseModel
from evaluation import load_raw_labels, evaluate, evaluation_level

random.seed(1)


class NETModel(BaseModel):
    def __init__(self, config):
        super(NETModel, self).__init__(config)

    def add_placeholders(self):
        """Define placeholders = entries to computational graph"""
        # shape = (batch size, max length of sentence in batch)
        self.word_ids = tf.compat.v1.placeholder(tf.int32, shape=[None, None],
                        name="word_ids")

        # shape = (batch size)
        self.mention_lengths = tf.compat.v1.placeholder(tf.int32, shape=[None],
                        name="mention_lengths")

        # shape = (batch size, max length of sentence, max length of word)
        self.char_ids = tf.compat.v1.placeholder(tf.int32, shape=[None, None, None],
                        name="char_ids")

        # shape = (batch_size, max_length of sentence)
        self.word_lengths = tf.compat.v1.placeholder(tf.int32, shape=[None, None],
                        name="word_lengths")

        self.mention_embedding = tf.placeholder("float", [None, self.config.timesteps,
                        self.config.bert_emb_len], name='mention_embedding_input')

        self.mention_bert_length = tf.compat.v1.placeholder(tf.int32, shape=[None],
                                                        name="mention_bert_lengths")

        self.targets = tf.compat.v1.placeholder(dtype=tf.float32, 
                    shape=[None, self.config.label_len_train], name="targets")

        self.targets_test = tf.compat.v1.placeholder(dtype=tf.float32, 
                    shape=[None, self.config.label_len_test], name="targets_test")

        self.targets_leve1 = tf.compat.v1.placeholder(dtype=tf.float32,
                    shape=[None, self.config.label_len_level1], name="target_level1")

        self.targets_leve2 = tf.compat.v1.placeholder(dtype=tf.float32,
                    shape=[None, self.config.label_len_level2], name="target_level2")
                              
        # label embedding matrix for training and testing data
        self.label_embedding_train = tf.compat.v1.constant(value=self.config.label_emb_train,
                    dtype=tf.float32, name="label_embedding_train")
        self.label_embedding_test = tf.compat.v1.constant(value=self.config.label_emb_test,
                    dtype=tf.float32, name="label_embedding_test")
        self.label_embedding_level1 = tf.compat.v1.constant(value=self.config.label_emb_level1,
                    dtype=tf.float32, name="label_embedding_level1")
        self.label_embedding_level2 = tf.compat.v1.constant(value=self.config.label_emb_level2,
                    dtype=tf.float32, name="label_embedding_level2")

        # hyper parameters
        self.dropout = tf.compat.v1.placeholder(dtype=tf.float32, shape=[],
                        name="dropout")
        self.lr = tf.compat.v1.placeholder(dtype=tf.float32, shape=[],
                        name="lr")


    def get_feed_dict(self, mention, mention_emb, label, bert_length, lr=None,
                      dropout=None, is_train="train"):
        """Given some data, pad it and build a feed dictionary

        Args:
            words: list of mention. A mention is a list of ids of a list of
                words. A word is a list of ids
            labels: list of ids
            lr: (float) learning rate
            dropout: (float) keep prob

        Returns:
            dict {placeholder: value}

        """
        is_decode = True
        char_ids, word_ids = get_batch_word_char_ids(mention, self.config.processing_word, is_decode)
        word_ids, sequence_lengths = pad_sequences(word_ids, 0, nlevels=1, timestep=self.config.timesteps)
        char_ids, word_lengths = pad_sequences(char_ids, pad_tok=0,
                                               nlevels=2, timestep=self.config.timesteps)

        # build feed dictionary
        feed = {self.word_ids: word_ids,
                self.mention_lengths: sequence_lengths,
                self.mention_embedding: mention_emb,
                self.mention_bert_length: bert_length,
                self.char_ids: char_ids,
                self.word_lengths: word_lengths}

        if is_train == "train":
            feed[self.targets] = label
        elif is_train == "test":
            feed[self.targets_test] = label
        elif is_train == "level1":
            feed[self.targets_leve1] = label
        elif is_train == "level2":
            feed[self.targets_leve2] = label

        if lr is not None:
            feed[self.lr] = lr

        if dropout is not None:
            feed[self.dropout] = dropout

        return feed, sequence_lengths

    def add_word_embeddings_op(self):
        """Defines self.word_embeddings

        If self.config.embeddings is not None and is a np array initialized
        with pre-trained word vectors, the word embeddings is just a look-up
        and we don't train the vectors. Otherwise, a random matrix with
        the correct shape is initialized.
        """
        with tf.compat.v1.variable_scope("words"):
            if self.config.embeddings is None:
                self.logger.info("WARNING: randomly initializing word vectors")
                _word_embeddings = tf.get_variable(
                        name="_word_embeddings",
                        dtype=tf.float32,
                        shape=[self.config.nwords, self.config.dim_word])
            else:
                _word_embeddings = tf.Variable(
                        self.config.embeddings,
                        name="_word_embeddings",
                        dtype=tf.float32,
                        trainable=False)
            word_embeddings = tf.nn.embedding_lookup(_word_embeddings,
                    self.word_ids, name="word_embeddings")

        with tf.compat.v1.variable_scope("chars"):
            # get char embeddings matrix
            _char_embeddings = tf.compat.v1.get_variable(
                    name="_char_embeddings",
                    dtype=tf.float32,
                    shape=[self.config.nchars, self.config.dim_char])
            char_embeddings = tf.nn.embedding_lookup(_char_embeddings,
                    self.char_ids, name="char_embeddings")

            # put the time dimension on axis=1
            s = tf.shape(char_embeddings)
            char_embeddings = tf.reshape(char_embeddings,
                    shape=[s[0]*s[1], s[-2], self.config.dim_char])
            word_lengths = tf.reshape(self.word_lengths, shape=[s[0]*s[1]])

            # bi lstm on chars
            cell_fw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_char,
                    state_is_tuple=True)
            cell_bw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_char,
                    state_is_tuple=True)
            _output = tf.nn.bidirectional_dynamic_rnn(
                    cell_fw, cell_bw, char_embeddings,
                    sequence_length=word_lengths, dtype=tf.float32)

            # read and concat output
            _, ((_, output_fw), (_, output_bw)) = _output
            output = tf.concat([output_fw, output_bw], axis=-1)

            # shape = (batch size, max sentence length, char hidden size)
            output = tf.reshape(output,
                    shape=[s[0], s[1], 2*self.config.hidden_size_char])
            word_embeddings = tf.concat([word_embeddings, output], axis=-1)

        self.word_embeddings = tf.nn.dropout(word_embeddings, self.dropout)


    def add_lstm_1_op(self):
        """Defines self.logits
        For each word in each mention of the batch
        """
        with tf.variable_scope("lstm_1"):  # bi-lstm
            cell_fw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_lstm_1)
            cell_bw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_lstm_1)
            (output_fw, output_bw), _ = tf.nn.bidirectional_dynamic_rnn(
                cell_fw, cell_bw, self.word_embeddings,
                sequence_length=self.mention_lengths, dtype=tf.float32)
            output = tf.concat([output_fw, output_bw], axis=-1)
            # lstm_output shape: [batch_size, time_step, hidden_dim * 2]
            lstm_1_output = tf.nn.dropout(output, self.dropout)
            # Start indices for each sample
            batch_size = tf.shape(lstm_1_output)[0]
            index = tf.range(0, batch_size) * self.config.timesteps + (self.mention_lengths - 1)
            # Indexing
            lstm_1_output = tf.reshape(lstm_1_output, [-1, 2 * self.config.hidden_size_lstm_1])
            self.lstm_1_output = tf.gather(lstm_1_output, index)


    def add_bert_embeddings_op(self):
        """Defines self.logits
        For each mention of the batch.
        """
        with tf.variable_scope("bert"):
            cell_fw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_bert)
            cell_bw = tf.contrib.rnn.LSTMCell(self.config.hidden_size_bert)
            (output_fw, output_bw), _ = tf.nn.bidirectional_dynamic_rnn(
                cell_fw, cell_bw, self.mention_embedding,
                sequence_length=self.mention_bert_length, dtype=tf.float32)
            output = tf.concat([output_fw, output_bw], axis=-1)
            bert_output = tf.nn.dropout(output, self.dropout)
            # Start indices for each sample
            batch_size = tf.shape(bert_output)[0]
            index = tf.range(0, batch_size) * self.config.timesteps + (self.mention_bert_length - 1)
            # Indexing
            bert_output = tf.reshape(bert_output, [-1, 2 * self.config.hidden_size_bert])
            self.bert_output = tf.gather(bert_output, index)
            # concat bert embedding and lstm_1_output
            # shape: [batch_size, time_step, hidden_dim * 2]
            self.concat_hidden = tf.concat([self.bert_output, self.lstm_1_output], axis=-1)


    def add_logits_op(self):
        """Defines self.logits
        """

        with tf.compat.v1.variable_scope("proj"):
            W = tf.get_variable("W", dtype=tf.float32,
                                shape=[2 * self.config.hidden_size_bert + 2* self.config.hidden_size_lstm_1, 
                                self.config.n_label_emb])

            b = tf.get_variable("b", shape=[self.config.n_label_emb],
                                dtype=tf.float32, initializer=tf.zeros_initializer())
            feature_layer = tf.matmul(self.concat_hidden, W) + b

        with tf.name_scope('Logits'):
            self.logits_train = tf.matmul(feature_layer, tf.transpose(self.label_embedding_train, perm=[1, 0]))
            self.logits_test = tf.matmul(feature_layer, tf.transpose(self.label_embedding_test, perm=[1, 0]))
            self.logits_level1 = tf.matmul(feature_layer, tf.transpose(self.label_embedding_level1, perm=[1, 0]))
            self.logits_level2 = tf.matmul(feature_layer, tf.transpose(self.label_embedding_level2, perm=[1, 0]))
            self.outputs_train = tf.nn.softmax(self.logits_train, name="outputs_train")
            self.outputs_test = tf.nn.softmax(self.logits_test, name="outputs_test")
            self.outputs_level1 = tf.nn.softmax(self.logits_level1, name="outputs_level1")
            self.outputs_level2 = tf.nn.softmax(self.logits_level2, name="outputs_level2")

        with tf.name_scope('Label_pred'):
            self.labels_pred_train = tf.cast(tf.argmax(self.outputs_train, axis=1), tf.int32)
            self.labels_pred_test = tf.cast(tf.argmax(self.outputs_test, axis=1), tf.int32)
            self.labels_pred_level1 = tf.cast(tf.argmax(self.outputs_level1, axis=1), tf.int32)
            self.labels_pred_level2 = tf.cast(tf.argmax(self.outputs_level2, axis=1), tf.int32)


    def add_loss_op(self):
        """Defines the loss"""
        pos_label = tf.ones_like(self.targets)
        neg_label = -tf.ones_like(self.targets)
        IND = tf.where(tf.equal(self.targets, 1), x=pos_label, y=neg_label)
        max_margin_loss = tf.maximum(0.0, 0.1 - tf.multiply(IND, self.outputs_train))
        self.loss = tf.reduce_mean(max_margin_loss)

        # for tensorboard
        tf.compat.v1.summary.scalar("loss", self.loss)

    def build(self):
        # NET specific functions

        self.add_placeholders()
        self.add_word_embeddings_op()
        self.add_lstm_1_op()
        self.add_bert_embeddings_op()
        self.add_logits_op()
        self.add_loss_op()

        # Generic functions that add training op and initialize session
        self.add_train_op(self.config.lr_method, self.lr, self.loss,
                          self.config.clip)
        self.initialize_session()  # now self.sess is defined and vars are init

    def predict_batch(self, mention, mention_emb, label, length):
        fd, sequence_lengths = self.get_feed_dict(mention, mention_emb, label, length,
                                                   dropout=1.0)
        labels_pred = self.sess.run(self.labels_pred_train, feed_dict=fd)
        labels_pred = load_raw_labels(self.config.supertypefile_common, labels_pred)
        return labels_pred, sequence_lengths

    def dev_data(self):
        self.dev = [self.config.mention_dev, self.config.embedding_dev,
                     self.config.label_dev, self.config.length_dev]

    def run_epoch(self, epoch):
        # progbar stuff for logging
        batch_size = self.config.batch_size
        nbatches = math.ceil(self.config.train_mention_length/batch_size) // batch_size
        prog = Progbar(target=nbatches)

        self.dev = (self.config.mention_dev, self.config.embedding_dev,
         self.config.label_dev, self.config.length_dev)
        dev_men, dev_x, dev_y, dev_length = self.sess.run(self.dev)
        total_loss = 0
        # iterate over dataset
        for i in range(nbatches):
            train = [self.config.mention_train, self.config.embedding_train,
                     self.config.label_train, self.config.length_train]
            batch_men, batch_x, batch_y, batch_bert_length = self.sess.run(train)
            fd, _ = self.get_feed_dict(batch_men, batch_x, batch_y, batch_bert_length,
                                       self.config.lr, self.config.dropout)

            _, label, train_loss, summary = self.sess.run(
                [self.train_op, self.labels_pred_train, self.loss, self.merged], feed_dict=fd)            
            prog.update(i + 1, [("train loss", train_loss)])
            total_loss += train_loss
            # tensorboard
            if i % 10 == 0:
                self.file_writer.add_summary(summary, epoch * nbatches + i)
        pred_dev, _ = self.predict_batch(dev_men, dev_x, dev_y, dev_length)
        strict_acc, mi_f1, ma_f1, _, _, _, _ = evaluate(pred_dev, dev_y)
        msg = "loss: {:.4f}, strict_acc: {:.4f}, mi_f1: {:.4f}, ma_f1: {:.4f}".format(
            total_loss, strict_acc, mi_f1, ma_f1
        )
        print("\nepoch:", epoch, "| dev evaluation:[", msg, "]")
        return total_loss

    def evaluate_all(self):
        test = [self.config.mention_test, self.config.embedding_test,
                self.config.label_test, self.config.length_test]
        mention, mention_emb, label, length = self.sess.run(test)
        fd, _ = self.get_feed_dict(mention, mention_emb, label, length,
                                                       dropout=1.0, is_train="test")

        labels_pred = self.sess.run(self.labels_pred_test, feed_dict=fd)
        write_file(self.config.dir_output + "/label.txt", list(labels_pred), label)
        labels_pred = load_raw_labels(self.config.supertypefile_common, labels_pred)
        strict_acc, mi_f1, ma_f1, mi_p, mi_r, ma_p, ma_r = evaluate(labels_pred, label)
        msg = "Test Evaluation on Overall---\n"
        msg += "Overall---strict_acc: {:.4f}, mi_f1: {:.4f}, ma_f1: {:.4f}, ".format(strict_acc, mi_f1, ma_f1)
        msg += "mi_p: {:.4f}, mi_r: {:.4f}, ma_p: {:.4f}, ma_r: {:.4f}\n".format(mi_p, mi_r, ma_p, ma_r)
        self.logger.info(msg)

    def evaluate_level1(self):
        level1 = [self.config.mention_level1, self.config.embedding_level1,
                self.config.label_level1, self.config.length_level1]
        (mention1, mention_emb1, label1, length1) = self.sess.run(level1)
        fd1, _ = self.get_feed_dict(mention1, mention_emb1, label1, length1, dropout=1.0, is_train="level1")
        labels_pred1 = self.sess.run(self.labels_pred_level1, feed_dict=fd1)
        strict_acc1, mi_f11, ma_f11, mi_p1, mi_r1, ma_p1, ma_r1 = evaluate(labels_pred1, label1)
        msg = "Test Evaluation on level_1---\n"
        msg += "Level_1---acc: {:.4f}, mi_f1: {:.4f}, ma_f1: {:.4f}, ".format(strict_acc1, mi_f11, ma_f11)
        msg += "mi_p: {:.4f}, mi_r: {:.4f}, ma_p: {:.4f}, ma_r: {:.4f}\n".format(mi_p1, mi_r1, ma_p1, ma_r1)
        self.logger.info(msg)

    def evaluate_level2(self):
        level2 = [self.config.mention_level2, self.config.embedding_level2,
                  self.config.label_level2, self.config.length_level2]
        (mention2, mention_emb2, label2, length2) = self.sess.run(level2)
        fd2, _ = self.get_feed_dict(mention2, mention_emb2, label2, length2, dropout=1.0, is_train="level2")
        labels_pred2 = self.sess.run(self.labels_pred_level2, feed_dict=fd2)
        strict_acc2, mi_f12, ma_f12, mi_p2, mi_r2, ma_p2, ma_r2 = evaluate(labels_pred2, label2)
        msg = "Test Evaluation on level_2---\n"
        msg += "Level_2---acc: {:.4f}, mi_f1: {:.4f}, ma_f1: {:.4f}, ".format(strict_acc2, mi_f12, ma_f12)
        msg += "mi_p: {:.4f}, mi_r: {:.4f}, ma_p: {:.4f}, ma_r: {:.4f}\n".format(mi_p2, mi_r2, ma_p2, ma_r2)
        self.logger.info(msg)







