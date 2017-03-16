import time
import datetime
import os
import tensorflow as tf
# from conf import config
import numpy as np
from model import RNN
from tensorflow.contrib.tensorboard.plugins import projector
from tensorflow.contrib import learn
import process_utils
import sys
import json


def init_vocabulary_processor(dx_train, dx_dev):
    """
    vocabulary processor
    implementation taken from 
    http://stackoverflow.com/questions/40661684/tensorflow-vocabularyprocessor#40741660
    """
    max_document_length = max([len(x.split(" ")) for x in dx_train])
    vocab_processor = learn.preprocessing.VocabularyProcessor(
        max_document_length)
    vocab_processor.fit(dx_train)
    x_train = np.array(list(vocab_processor.transform(dx_train)))
    x_dev = np.array(list(vocab_processor.transform(dx_dev)))

    vocab_dict = vocab_processor.vocabulary_._mapping
    sorted_vocab = sorted(vocab_dict.items(), key=lambda x: x[1])
    vocabulary = list(list(zip(*sorted_vocab))[0])

    return x_train, x_dev, vocab_dict, vocabulary


def init_embeddings(config, pretrained_embeddings, vocabulary):
    init_embd = config['std_dev'] * np.random.randn(
        len(config['word_vector_type']) + 1,
        len(vocabulary), config['dim_proj']
    )
    if pretrained_embeddings:
        for index_3d, stored_embedding in enumerate(pretrained_embeddings):
            counts = 0
            mappings = {}
            for index, entry in enumerate(vocabulary):
                if entry in stored_embedding.word_to_index:
                    vec_index = stored_embedding.word_to_index[entry]
                    mappings[vec_index] = index
                    counts += 1
                    init_embd[index_3d, index] = \
                        stored_embedding.vectors[vec_index]
            print (" Found {} words in pretrained vectors {} out of {}".format(
                counts, config['word_vector_type'][index_3d], len(vocabulary)))
            stored_embedding.set_mappings(mappings)
    return init_embd


def set_train(sess, config, data, pretrained_embeddings=[]):

    dx_train, y_train, dx_dev, y_dev = data

    x_train, x_dev, vocab_dict, vocabulary = init_vocabulary_processor(
        dx_train, dx_dev)

    print("Vocabulary Size: {}".format(len(vocabulary)))
    print("Train/Dev split: {}/{},{}".format(
        len(y_train), len(y_dev), len(y_train) + len(y_dev)))

    # Build word embeddings
    config['n_words'] = len(vocabulary)
    config['sentence_len'] = x_train.shape[1]

    word_embd_tensor = init_embeddings(
        config, pretrained_embeddings, vocabulary)

    # Output directory for models and summaries
    timestamp = str(int(time.time()))
    out_dir = os.path.abspath(os.path.join(os.path.curdir, "runs", timestamp))
    config['out_dir'] = out_dir
    print("Writing to {}\n".format(out_dir))

    # network = RNN(config, sess, init_embd)
    network = RNN(config, sess, word_embd_tensor)

    dev_summary_dir = os.path.join(out_dir, "summaries", "dev")
    dev_summary_writer = tf.summary.FileWriter(
        dev_summary_dir, sess.graph)

    train_summary_dir = os.path.join(out_dir, "summaries", "train")
    train_summary_writer = tf.summary.FileWriter(
        train_summary_dir, sess.graph)

    tf.train.Saver(tf.global_variables())

    sess.run(tf.global_variables_initializer())

    # train fucntion
    def train_step(x_batch, y_batch, iter_):
        # print("batch lenght {}". format(len(x_batch)))
        feed_dict = {
            network.x: x_batch,
            network.y: y_batch,
            network.dropout_prob: config["dropout_rate"],
            # network.str_summary_type: "",
            network.input_keep_prob: config["keep_prob_inp"],
            network.output_keep_prob: config["keep_prob_out"],
            network.seq_lengths: len(x_batch) * [config['n_words']],
            network.batch_size: len(x_batch),
            network.metrics_weight: 1,
            network.fixed_acc_value: 0,
            network.fixed_loss_value: 0
            # network.train_phase: True
        }

        if (iter_ % 100 == 99):  # record full summaries:
            run_options = tf.RunOptions(
                trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()
            output_ = [network.update, network.global_step,
                       network.accuracy, network.mean_loss,
                       network.summary_op]
            _, current_step, accuracy, loss, net_sum = sess.run(
                output_, feed_dict, options=run_options,
                run_metadata=run_metadata)

            train_summary_writer.add_run_metadata(
                run_metadata, 'step%d' % current_step)
        else:
            output_ = [network.update, network.global_step,
                       network.accuracy, network.mean_loss,
                       network.summary_op]
            _, current_step, accuracy, loss, net_sum = sess.run(output_, feed_dict)

        if config['save_step'] == current_step:
            # save word embeddings
            emb_m = sess.run([network.w_embeddings], feed_dict)
            save_embedding(emb_m)
        # write train summary
        train_summary_writer.add_summary(net_sum, current_step)

        time_str = datetime.datetime.now().isoformat()
        print("{}: step {}, loss {}, acc {}, b_len {}".format(
            time_str, current_step, loss, accuracy, len(x_batch)))

        # train_summary_writer.add_summary(summaries, step)
        # grad_summaries_writer.add_summary(grad_summary, step)
        if current_step % config['evaluate_every'] == 0:
            dev_step(x_dev, y_dev)

    def dev_step(x_batch, y_batch):
        if config['split_dev']:  # will need to split dev set to smaller chunks
            mini_size = config['dev_minibatch']
            acc_sum = 0
            loss_sum = 0
            for i in range(0, len(x_batch), mini_size):
                if (i + mini_size < len(x_batch)):
                    mini_x_batch = x_batch[i:i + mini_size]
                    mini_y_batch = y_batch[i:i + mini_size]
                else:
                    mini_x_batch = x_batch[i:]
                    mini_y_batch = y_batch[i:]
                feed_dict = {
                    network.x: mini_x_batch,
                    network.y: mini_y_batch,
                    network.dropout_prob: 1.0,
                    network.input_keep_prob: config["keep_prob_inp"],
                    network.output_keep_prob: config["keep_prob_out"],
                    network.seq_lengths: len(mini_x_batch) * [config['n_words']],
                    network.batch_size: len(mini_x_batch),
                    network.metrics_weight: 1,
                    network.fixed_acc_value: 0,
                    network.fixed_loss_value: 0
                }
                output_ = [network.global_step, network.accuracy,
                           network.mean_loss]
                current_step, accuracy, loss = sess.run(output_, feed_dict)
                acc_sum += len(mini_x_batch) * accuracy
                loss_sum += len(mini_x_batch) * loss

            loss = loss_sum / len(x_batch)
            accuracy = acc_sum / len(x_batch)
            print ("           loss {} accuracy{}".format(loss, accuracy))
            feed_dict = {
                    network.x: mini_x_batch,
                    network.y: mini_y_batch,
                    network.dropout_prob: 1.0,
                    network.input_keep_prob: config["keep_prob_inp"],
                    network.output_keep_prob: config["keep_prob_out"],
                    network.seq_lengths: len(mini_x_batch) * [config['n_words']],
                    network.batch_size: len(mini_x_batch),
                    network.metrics_weight: 0.0,
                    network.fixed_acc_value: accuracy,
                    network.fixed_loss_value: loss
            }
        else:  # use whole batch
            feed_dict = {
                network.x: x_batch,
                network.y: y_batch,
                network.dropout_prob: 1.0,
                # network.str_summary_type: "",
                network.input_keep_prob: config["keep_prob_inp"],
                network.output_keep_prob: config["keep_prob_out"],
                network.seq_lengths: len(x_batch) * [config['n_words']],
                network.batch_size: len(x_batch),
                network.metrics_weight: 1,
                network.fixed_acc_value: 0,
                network.fixed_loss_value: 0
                # network.train_phase: False
            }
        output_ = [network.global_step, network.accuracy,
                   network.mean_loss, network.summary_op]
        current_step, accuracy, loss, net_sum = sess.run(
            output_, feed_dict)
        # save summary
        dev_summary_writer.add_summary(net_sum, current_step)

        print("\nEvaluation dev set:")
        time_str = datetime.datetime.now().isoformat()
        print("{}: step {}, loss {}, acc {}, b_len {}\n".format(
            time_str, current_step, loss, accuracy, len(x_batch)))
        # if writer:
        #     writer.add_summary(summaries, step)

    def save_embedding(embd_matrix):
        summary_path = os.path.join(out_dir, 'summaries', 'embeddings')
        if not os.path.exists(summary_path):
            os.makedirs(summary_path)
        # store metadata
        metadata_path = os.path.join(
            summary_path, 'metadata.tsv')
        with open(metadata_path, 'w') as metadata_file:
            for row in vocabulary:
                metadata_file.write('{}\n'.format(row))

        embd_tensor = []

        writer = tf.summary.FileWriter(summary_path, sess.graph)
        configuration = projector.ProjectorConfig()
        for i_, sub_emb_tensor in enumerate(embd_matrix):
            w_var = tf.Variable(sub_emb_tensor, name='embd_' + str(i_))
            embd_tensor.append(w_var)
            sess.run(w_var.initializer)

            embedding = configuration.embeddings.add()
            embedding.tensor_name = w_var.name
            embedding.metadata_path = metadata_path
            projector.visualize_embeddings(
                writer, configuration)
        sess.run(embd_tensor)
        saver = tf.train.Saver(embd_tensor)
        saver.save(sess, os.path.join(
            summary_path, 'embedding_.ckpt'))



    # Generate batches
    print ("About to build batches for x:{} with number of words".format(
        len(x_train), config['n_words']))
    batches = process_utils.batch_iter(
        list(zip(x_train, y_train)), config['batch_size'], config['n_epochs'])

    conf_path = os.path.abspath(os.path.join(out_dir, "config.json"))
    json.dump(config, open(conf_path, 'w'), indent="\t")
    print("Saved configuration file at: {}".format(conf_path))

    print ("train loop starting for every batch")
    for iter_, batch in enumerate(batches):
        x_batch, y_batch = zip(*batch)
        train_step(x_batch, y_batch, iter_)
        # current_step = tf.train.global_step(sess, global_step)
        # if current_step % config['evaluate_every'] == 0:
        #     print("\nEvaluation:")
        #     dev_step(x_dev, y_dev, writer=dev_summary_writer)
        #     print("")
        # if current_step % config['checkpoint_every'] == 0:
        #     path = saver.save(
        #         sess, checkpoint_prefix, global_step=current_step)
        #     print("Saved model checkpoint to {}\n".format(path))
