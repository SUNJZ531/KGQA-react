from dataloader import DataLoader
from QA_model import QuestionAnswerModel
import torch
import argparse
import logging
import tqdm
from pytorch_transformers import AdamW
import os
import time
from graph_manager import MyGraph
from negative_manager import NegativeManager
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser()

parser.add_argument("--bert_path", type=str, default="C:/Users/yeeeqichen/Desktop/语言模型/")
parser.add_argument("--bert_name", type=str, default="roberta-base")
parser.add_argument("--use_LSTM", action='store_true', default=False)
parser.add_argument("--embed_model_path", type=str, default="./checkpoint/")
parser.add_argument("--embed_method", type=str, default='rotatE')
parser.add_argument("--train_file", type=str, default="./MetaQA/QA_data/qa_train_1hop.txt")
parser.add_argument("--valid_file", type=str, default="./MetaQA/QA_data/qa_dev_1hop.txt")
parser.add_argument("--test_file", type=str, default="./MetaQA/QA_data/qa_test_1hop.txt")
parser.add_argument("--dict_path", type=str, default="./MetaQA/QA_data/entities.dict")
parser.add_argument("--relation_file", type=str, default='./MetaQA/KGE_data/relation2id.txt')
parser.add_argument("--batch_size", type=int, default=4)
parser.add_argument("--seq_length", type=int, default=20)
parser.add_argument("--EPOCH", type=int, default=10)
parser.add_argument("--valid_steps", type=int, default=1000)
parser.add_argument("--log_level", type=str, default="DEBUG")
parser.add_argument("--require_improvement", type=int, default=100)
parser.add_argument("--save_path", type=str, default='/model/' + time.strftime("%Y-%m-%d__%H-%M-%S", time.localtime()))
parser.add_argument("--require_save", action='store_true', default=True)
parser.add_argument("--lr", default=2e-5, type=float)
parser.add_argument("--weight_decay", default=0.0, type=float)
parser.add_argument("--gamma", type=float, default=0.95)
parser.add_argument("--adam_epsilon", default=1e-8, type=float)
parser.add_argument("--max_gradient_norm", type=int, default=10)
parser.add_argument("--scheduler_steps", type=int, default=100)
parser.add_argument("--plot_steps", type=int, default=1000)
parser.add_argument("--continue_best_model", action='store_true', default=False)
parser.add_argument("--negative_sampling_rate", type=float, default=1.0)
parser.add_argument("--negative_sampling_size", type=int, default=25)
parser.add_argument("--n_clusters", type=int, default=8)
parser.add_argument("--use_cluster", action='store_true', default=False)
parser.add_argument("--fine_tune", action="store_true", default=False)
parser.add_argument("--negs_thresh_hold", type=int, default=15)
parser.add_argument("--not_attention", action='store_true', default=False)
parser.add_argument("--not_dnn", action='store_true', default=False)
parser.add_argument("--attention_method", type=str, default='mine')


args = parser.parse_args()

save_path = os.getcwd() + args.save_path
attention_save_path = save_path + '/attention'
if not os.path.exists(save_path):
    os.makedirs(save_path)
    os.makedirs(attention_save_path)


logger = logging.getLogger(__name__)
logger.setLevel(args.log_level)
formatter = logging.Formatter('%(asctime)s -- %(levelname)s - %(name)s - %(message)s')

sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)

fh = logging.FileHandler(save_path + '/log.txt')
fh.setFormatter(formatter)
logger.addHandler(fh)

graph = MyGraph()
negative_manager = NegativeManager(thresh_hold=args.negs_thresh_hold)


def train(model, data_loader):
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {
            'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
            'weight_decay': 0.01
        },
        {
            'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)],
            'weight_decay': 0.0
        }
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="max", factor=0.95, patience=2, verbose=True)
    best_performance = {'hits_1': 0, 'hits_3': 0, 'hits_10': 0}
    require_improvement = 0
    total_loss = []
    model.train()
    for i in range(args.EPOCH):
        steps = 1
        graph.train_loss.append([])
        graph.average_train_loss.append([])
        graph.negative_loss.append([])
        graph.positive_loss.append([])
        graph.hits_1.append([])
        graph.hits_3.append([])
        graph.hits_10.append([])
        total_norms = []
        for case_ids, question_token_ids, question_masks, head_id, answers_id, negative_ids \
                in tqdm.tqdm(data_loader.batch_generator('train')):
            model.zero_grad()
            scores = model(question_token_ids, question_masks, head_id).cpu()
            cur_loss = []
            # 一边搜集优质negative_sample，一边用negative_sample训练

            # 先更新
            negative_manager.step(scores, answers_id, case_ids)
            # 后采样
            negative_ids = negative_manager.get_negative_samples(case_ids)
            # print(negative_ids)
            # 下面计算一个batch内的loss
            for score, answers, negatives in zip(scores, answers_id, negative_ids):
                # 每次循环计算一个（h, r, t)的loss，分为positive和negative两部分
                positive_scores = torch.index_select(score, 0, torch.tensor(answers))
                negative_scores = torch.index_select(-score, 0, torch.tensor(negatives))
                # target_scores = torch.index_select(score, 0, torch.tensor(answers))
                # print(positive_scores, sum(target_scores))
                positive_loss = torch.sum(-torch.log(torch.sigmoid(positive_scores)))
                negative_loss = torch.sum(-torch.log(torch.sigmoid(negative_scores)))

                graph.positive_loss[i].append((steps, positive_loss.detach()))
                graph.negative_loss[i].append((steps, negative_loss.detach()))

                cur_loss.append(positive_loss + negative_loss)
                # logger.debug(cur_loss)
                # loss.append(torch.sum(target_scores))
                # loss.append(torch.sum(-torch.log(target_scores)))
            train_loss = torch.stack(cur_loss).mean()
            total_loss.append(train_loss)
            graph.train_loss[i].append((steps, train_loss.detach()))
            if steps % args.scheduler_steps == 0:
                average_loss = torch.stack(total_loss).mean()
                average_norm = torch.stack(total_norms).mean()
                graph.average_train_loss[i].append((steps, average_loss.detach()))
                logger.info('EPOCH: {}, STEP: {}, average loss: {}, average norm: {}'
                            .format(i, steps, average_loss, average_norm))
                total_loss = []
            train_loss.backward()
            # 统计一下训练过程中的梯度情况，来作为梯度裁剪参数的依据
            temp = []
            for p in model.parameters():
                # print(p)
                if p.requires_grad and p.grad is not None:
                    temp.append(torch.norm(p.grad.detach().to(model.device), 2.0))
            total_norm = torch.norm(torch.stack(temp), 2.0)
            # total_norm = torch.norm(torch.stack([torch.norm(p.grad.detach().to(model.device), 2.0)
            #                                      for p in model.parameters()]), 2.0)
            total_norms.append(total_norm)
            # 进行梯度裁剪
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=args.max_gradient_norm)
            optimizer.step()
            if steps % args.valid_steps == 0:
                logger.info('Start validating...')
                hits_1 = 0
                hits_3 = 0
                hits_10 = 0
                cnt = 0
                with torch.no_grad():
                    for _question, _mask, _head, _answers in data_loader.batch_generator('valid'):
                        if not args.use_cluster:
                            scores = model(_question, _mask, _head).cpu()
                            predicts = torch.sort(scores, dim=1, descending=True).indices
                            for predict, _answer in zip(predicts, _answers):
                                cnt += 1
                                for j in range(10):
                                    if predict[j] in _answer:
                                        if j == 0:
                                            hits_1 += 1
                                        if j < 3:
                                            hits_3 += 1
                                        if j < 10:
                                            hits_10 += 1
                                        break
                        else:
                            scores, indices = model(_question, _mask, _head, use_cluster=True)
                            predicts = []
                            for _scores in scores:
                                predicts.append(torch.sort(_scores, dim=1, descending=True).indices.squeeze(0)[:10])
                            for predict, _answer, cluster_index in zip(predicts, _answers, indices):
                                # print(predict, cluster_index)
                                cnt += 1
                                for rank, j in enumerate(predict):
                                    if model.cluster2ent[cluster_index][j] in _answer:
                                        if rank == 0:
                                            hits_1 += 1
                                        if rank < 3:
                                            hits_3 += 1
                                        if rank < 10:
                                            hits_10 += 1
                                        break
                cur_performance = {'hits_1': hits_1 / cnt, 'hits_3': hits_3 / cnt, 'hits_10': hits_10 / cnt}
                logger.info('EPOCH: {}, STEP: {}, Hits_1: {}, Hits_3: {}, Hits_10: {}'
                            .format(i, steps, cur_performance['hits_1'], cur_performance['hits_3'],
                                    cur_performance['hits_10']))
                graph.hits_1[i].append((steps, hits_1 / cnt))
                graph.hits_3[i].append((steps, hits_3 / cnt))
                graph.hits_10[i].append((steps, hits_10 / cnt))
                # 依据验证集上的表现来调整学习率
                # scheduler.step(cur_performance['hits_1'])
                graph.lr.append((optimizer.param_groups[0]['lr'], optimizer.param_groups[1]['lr']))
                if cur_performance['hits_1'] > best_performance['hits_1']:
                    best_performance = cur_performance
                    if args.require_save:
                        logger.info('Saving model...')
                        torch.save(model.state_dict(), save_path + '/model.pkl')
                        with open(save_path + '/performance.txt', 'w') as f:
                            f.write("Best Performance, Hits_1: {}, Hits_3: {}, Hist_10: {}".
                                    format(best_performance['hits_1'], best_performance['hits_3'],
                                           best_performance['hits_10']))
                        with open(save_path + '/config.txt', 'w') as f:
                            for eachArg, value in args.__dict__.items():
                                f.writelines(eachArg + ' : ' + str(value) + '\n')
                    require_improvement = 0
                else:
                    require_improvement += 1
                    if require_improvement == args.require_improvement:
                        logger.warning('EXIT: training finished because of no improvement')
                        exit(-1)
            if steps % args.plot_steps == 0:
                def _init_figure(title, x_label, y_label):
                    plt.figure()
                    plt.grid(True)
                    # plt.axis('tight')
                    plt.title(title)
                    plt.xlabel(x_label)
                    plt.ylabel(y_label)
                _init_figure(title='train_loss', x_label='Steps', y_label='Train_loss')
                plt.plot([_[0] for _ in graph.train_loss[i]], [_[1].tolist() for _ in graph.train_loss[i]])
                plt.savefig(save_path + '/train_loss_EPOCH{}.png'.format(i))
                plt.close()
                _init_figure(title='average_train_loss', x_label='Steps', y_label='Average_train_loss')
                plt.plot([_[0] for _ in graph.average_train_loss[i]], [_[1].tolist() for _ in graph.average_train_loss[i]])
                plt.savefig(save_path + '/average_train_loss_EPOCH{}.png'.format(i))
                plt.close()
                _init_figure(title='performance', x_label='Steps', y_label='Percentage')
                _x = [_[0] for _ in graph.hits_1[i]]
                plt.plot(_x, [_[1] for _ in graph.hits_1[i]], label='hits_1')
                plt.plot(_x, [_[1] for _ in graph.hits_3[i]], label='hits_3')
                plt.plot(_x, [_[1] for _ in graph.hits_10[i]], label='hits_10')
                plt.legend()
                plt.savefig(save_path + '/performance_EPOCH{}.png'.format(i))
                plt.close()
                _init_figure(title='positive_loss and negative_loss', x_label='Steps', y_label='Loss')
                _x = [_[0] for _ in graph.positive_loss[i]]
                plt.plot(_x, [_[1].tolist() for _ in graph.positive_loss[i]], label='positive')
                plt.plot(_x, [_[1].tolist() for _ in graph.negative_loss[i]], label='negative')
                plt.legend()
                plt.savefig(save_path + '/positive_and_negative_loss{}.png'.format(i))
                plt.close()
                # _init_figure(title='attention_weights', x_label='question_tokens', y_label='weights')
                # # [(batch_size, sequence_length), (batch_size, sequence_length)]
                # target_data = [_[-1] for _ in model.relation_predictor.attention_scores[-1]]
                # model.attention_scores = []
                # plt.bar([i for i in range(args.seq_length)], target_data[1].detach().cpu().squeeze(1))
                # plt.savefig(attention_save_path + '/EPOCH{}_steps{}.png'.format(i, steps))
                # plt.close()

            steps += 1
    logger.info('finish training')


def main():
    embed_model_path = args.embed_model_path + args.embed_method + '.ckpt'
    model = QuestionAnswerModel(embed_model_path=embed_model_path, embed_method=args.embed_method,
                                attention=not args.not_attention, bert_path=args.bert_path, bert_name=args.bert_name,
                                n_clusters=args.n_clusters, fine_tune=args.fine_tune, use_lstm=args.use_LSTM,
                                use_dnn=not args.not_dnn, attention_method=args.attention_method)
    if args.continue_best_model:
        path = 'model/2021-03-17__13-05-53/model.pkl'
        logger.info('continue training, loading model stat_dict from {}'.format(path))
        model.load_state_dict(torch.load(path))
    total_param = 0
    for name, param in model.named_parameters():
        num = 1
        for size in param.shape:
            num *= size
        total_param += num
        logger.info("{:30s} : {}, require_grad: {}".format(name, param.shape, param.requires_grad))
    logger.info("total param num {}".format(total_param))
    data_loader = DataLoader(
        train_file=args.train_file,
        valid_file=args.valid_file,
        test_file=args.test_file,
        batch_size=args.batch_size,
        seq_length=args.seq_length,
        dict_path=args.dict_path,
        bert_path=args.bert_path,
        bert_name=args.bert_name,
        negative_sample_rate=args.negative_sampling_rate,
        negative_sample_size=args.negative_sampling_size,
    )
    model.to(model.device)
    train(model, data_loader)


if __name__ == '__main__':
    main()
