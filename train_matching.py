import sys
import argparse
import os
import json
import torch
from time import time
from datetime import timedelta
from os.path import join, exists
from torch.optim import Adam

from utils import read_jsonl, get_data_path, get_result_path

from dataloader import MatchSumPipe
from model import MatchSum
from metrics import MarginRankingLoss, ValidMetric, MatchRougeMetric
from callback import MyCallback
from fastNLP.core.trainer import Trainer
from fastNLP.core.tester import Tester
from fastNLP.core.callback import SaveModelCallback

def configure_training(args):
    devices = [int(gpu) for gpu in args.gpus.split(',')]
    params = {}
    params['encoder']       = args.encoder
    params['candidate_num'] = args.candidate_num
    params['batch_size']    = args.batch_size
    params['accum_count']   = args.accum_count
    params['max_lr']        = args.max_lr
    params['margin']        = args.margin
    params['warmup_steps']  = args.warmup_steps
    params['n_epochs']      = args.n_epochs
    params['valid_steps']   = args.valid_steps
    params['data_path']   = args.data_path
    return devices, params

def train_model(args):
    
    # check if the data_path and save_path exists
    data_paths = get_data_path(args.mode, args.encoder)
    for name in data_paths:
        assert exists(data_paths[name])
    if not exists(args.save_path):
        os.makedirs(args.save_path)
    
    # load summarization datasets
    datasets = MatchSumPipe(args.candidate_num, args.encoder).process_from_file(data_paths)
    print('Information of dataset is:')
    print(datasets)
    train_set = datasets.datasets['train']
    valid_set = datasets.datasets['val']
    
    # configure training
    devices, train_params = configure_training(args)
    with open(join(args.save_path, 'params.json'), 'w') as f:
        json.dump(train_params, f, indent=4)
    print('Devices is:')
    print(devices)

    # configure model
    model = MatchSum(args.candidate_num, args.encoder)
    optimizer = Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=0)
    
    callbacks = [MyCallback(args), 
                 SaveModelCallback(save_dir=args.save_path, top=5)]
    
    criterion = MarginRankingLoss(args.margin)
    val_metric = [ValidMetric(save_path=args.save_path, data=read_jsonl(data_paths['val']))]
    
    assert args.batch_size % len(devices) == 0
    
    trainer = Trainer(train_data=train_set, model=model, optimizer=optimizer,
                      loss=criterion, batch_size=args.batch_size,
                      update_every=args.accum_count, n_epochs=args.n_epochs, 
                      print_every=10, dev_data=valid_set, metrics=val_metric, 
                      metric_key='ROUGE', validate_every=args.valid_steps, 
                      save_path=args.save_path, device=devices, callbacks=callbacks)
    
    print('Start training with the following hyper-parameters:')
    print(train_params)
    trainer.train()


def test_model(args):

    models = os.listdir(args.save_path)
    
    # load dataset
    # data_paths = get_data_path(args.mode, args.encoder)
    data_paths = {'test': args.data_path}
    
    datasets = MatchSumPipe(args.candidate_num, args.encoder).process_from_file(data_paths)
    print('Information of dataset is:')
    print(datasets)
    test_set = datasets.datasets['test']
    
    # need 1 gpu for testing
    device = int(args.gpus)
    
    args.batch_size = 1

    for cur_model in models:
        
        print('Current model is {}'.format(cur_model))

        # load model
        model = torch.load(join(args.save_path, cur_model))
    
        # configure testing
        jsonl_name = args.data_path.split('/')[-1].split('.')[0]
        print(f'jsonl_name: {jsonl_name}')
        dec_path, ref_path = get_result_path(args.save_path, cur_model, jsonl_name)
        test_metric = MatchRougeMetric(data=read_jsonl(data_paths['test']), dec_path=dec_path, 
                                  ref_path=ref_path, n_total = len(test_set))
        tester = Tester(data=test_set, model=model, metrics=[test_metric], 
                        batch_size=args.batch_size, device=device, use_tqdm=False)
        tester.test()


def rouge(args):
    models = os.listdir(args.save_path)
    for cur_model in models:
        if args.eval_path:
            eval_path = args.eval_path
        else:
            jsonl_name = args.data_path.split('/')[-1].split('.')[0]
            print(f'jsonl_name: {jsonl_name}')
            
            result_path = join(args.save_path, '../result')
            if not exists(result_path):
                raise FileNotFoundError

            eval_path = join(result_path, cur_model, jsonl_name)
        
            if not exists(eval_path):
                raise FileNotFoundError
        
        dec_path = join(eval_path, 'dec')
        ref_path = join(eval_path, 'ref')
        print(f'dec_path: {dec_path}')
        print(f'ref_path: {ref_path}')
        MatchRougeMetric.eval_rouge(dec_path, ref_path, Print=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='training/testing of MatchSum'
    )
    parser.add_argument('--mode', required=True,
                        help='training or testing of MatchSum', type=str)

    parser.add_argument('--save_path', required=True,
                        help='root of the model', type=str)
    parser.add_argument('--data_path', required=True,
                        help='path of the data (for testing)', type=str)
    
    parser.add_argument('--eval_path', required=False,
                        help='path of the data (for ROUGE eval)', type=str)
                    
    # example for gpus input: '0,1,2,3'
    parser.add_argument('--gpus', required=True,
                        help='available gpus for training(separated by commas)', type=str)
    parser.add_argument('--encoder', required=True,
                        help='the encoder for matchsum (bert/roberta)', type=str)

    parser.add_argument('--batch_size', default=16,
                        help='the training batch size', type=int)
    parser.add_argument('--accum_count', default=2,
                        help='number of updates steps to accumulate before performing a backward/update pass', type=int)
    parser.add_argument('--candidate_num', default=20,
                        help='number of candidates summaries', type=int)
    parser.add_argument('--max_lr', default=2e-5,
                        help='max learning rate for warm up', type=float)
    parser.add_argument('--margin', default=0.01,
                        help='parameter for MarginRankingLoss', type=float)
    parser.add_argument('--warmup_steps', default=10000,
                        help='warm up steps for training', type=int)
    parser.add_argument('--n_epochs', default=5,
                        help='total number of training epochs', type=int)
    parser.add_argument('--valid_steps', default=1000,
                        help='number of update steps for validation and saving checkpoint', type=int)

    args = parser.parse_known_args()[0]
    
    if args.mode == 'train':
        print('Training process of MatchSum !!!')
        train_model(args)
    elif args.mode == 'test':
        print('Testing process of MatchSum !!!')
        test_model(args)
    elif args.mode == 'rouge':
        rouge(args)
