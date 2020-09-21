import os
import torch
import argparse
import time

def DefaultSetup():
    parser=argparse.ArgumentParser()
    parser.add_argument('--name', type=str, default='TheModel', help="Name of the current execution")
    # Calibrate the batch batch_size, crop_size and patch_size
    parser.add_argument('--batch_size', type=int, default=16, help='input batch size (One of the aspects that can be used to control GPU requirements)')
    parser.add_argument('--crop_size', type=int, default=340, help='Crop the images to this new size')
    parser.add_argument('--patch_size', type=int, default=32, help='specifies the size of the patch that we are going to use')
    # This can be modified according to the number of GPU's used to train the model
    parser.add_argument('--gpu_ids', type=str, default='0', help="Used to specify the ID's of the GPU's to be used (e.g 0,1 if 2 GPU's are used)")

    parser.add_argument('--checkpoints_dir', type=str, default='/content/drive/My Drive/MyResearch/', help='models are saved here')
    # Experiment with this being Batch as well!
    parser.add_argument('--norm_type', type=str, default='batch', help='instance normalization or batch normalization')
    parser.add_argument('--num_downs',type=int, default=9,help=' How many U-net modules are created in the generator')
    # THe below variable is actually useless but remember the reasoning behind it!
    parser.add_argument('--use_ragan', default=True, action='store_true', help='use ragan')
    parser.add_argument('--n_layers_D', type=int, default=5, help='number of layers in global discriminator')
    parser.add_argument('--n_layers_patchD', type=int, default=4, help='number of layers in local discriminator')
    parser.add_argument('--patchD_3', type=int, default=6, help='Number of patches to crop for the local discriminator')
    # To be in accordance with EGAN, change above to 6 ( When not using the individual patch)
    parser.add_argument('--patch_vgg', default=True, action='store_true', help='use vgg loss between each patch')
    parser.add_argument('--hybrid_loss', default=True, action='store_true', help='use lsgan and ragan separately')

    return parser

def TrainingSetup(the_args):
    # EGAN used 0.0001 but Radford recommended 0.0002
    # Below does not need to be printed
    the_args.add_argument('--display_freq', type=int, default=30, help='frequency of showing training results on screen')
    the_args.add_argument('--print_freq', type=int, default=100, help='frequency of showing training results on console')
    the_args.add_argument('--save_latest_freq', type=int, default=5000, help='frequency of saving the latest results')
    the_args.add_argument('--save_epoch_freq', type=int, default=5, help='frequency of saving checkpoints at the end of epochs')
    the_args.add_argument('--phase', type=str, default='train', help='train, val, test, etc')
    the_args.add_argument('--niter', type=int, default=100, help='# of iter at starting learning rate')
    the_args.add_argument('--niter_decay', type=int, default=100, help='# of iter to linearly decay learning rate to zero')
    the_args.add_argument('--beta1', type=float, default=0.5, help='momentum term of adam')
    the_args.add_argument('--lr', type=float, default=0.00015, help='initial learning rate for adam')

    return the_args

def process(the_args): # I dont need to be printing the display and other useless information
    opt = the_args.parse_args()

    str_ids = opt.gpu_ids.split(',')
    opt.gpu_ids = []
    for str_id in str_ids:
        id = int(str_id)
        if id >= 0:
            opt.gpu_ids.append(id)

    if len(opt.gpu_ids) > 0:
        torch.cuda.set_device(opt.gpu_ids[0])

    args = vars(opt)

    expr_dir = os.path.join(opt.checkpoints_dir, "TheModel")
    if(os.path.isdir(expr_dir)==False):
        os.mkdir(expr_dir)
    file_name = os.path.join(expr_dir, 'config.txt')

    with open(file_name, 'wt') as opt_file:
        opt_file.write('------------ Options -------------\n')# First statement will print to the file
        print('------------ Options -------------')# Second statement will print to terminal

        for k, v in sorted(args.items()):
            opt_file.write('%s: %s\n' % (str(k), str(v)))
            print('%s: %s' % (str(k), str(v)))

        opt_file.write('-------------- End ----------------\n')
        print('-------------- End ----------------')

    web_dir = os.path.join(opt.checkpoints_dir, opt.name, 'web')
    opt.img_dir = os.path.join(opt.web_dir, 'images')
    print('create web directory %s...' % opt.web_dir)
    if(os.path.isdir(opt.web_dir)==False):
        os.mkdir(opt.web_dir)
    if(os.path.isdir(opt.img_dir)==False):
        os.mkdir(opt.img_dir)
    opt.log_name = os.path.join(opt.checkpoints_dir, opt.name, 'loss_log.txt')
    with open(opt.log_name, "a") as log_file:
        now = time.strftime("%c")
        log_file.write('================ Training Loss (%s) ================\n' % now)

    opt.save_dir=os.path.join(opt.checkpoints_dir,opt.name)
    return opt
